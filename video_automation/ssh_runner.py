"""
Модуль для выполнения команд на поде RunPod и передачи файлов.

ВАЖНОЕ ИЗМЕНЕНИЕ: раньше использовалась библиотека paramiko, но она
периодически "зависала" на некоторых серверах без вывода ошибки - хотя
тот же самый системный ssh.exe подключался мгновенно. Поэтому теперь
весь модуль использует СИСТЕМНЫЙ ssh.exe и scp.exe (те же самые
программы, которыми ты подключаешься вручную) - надёжнее.

ИСПОЛЬЗОВАНИЕ (для проверки):
    set RUNPOD_API_KEY=твой_ключ
    python ssh_runner.py "echo hello from server"

ТРЕБОВАНИЯ: системные ssh.exe и scp.exe (есть по умолчанию на Windows 10/11).
"""

import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from runpod_controller import get_latest_pod, get_ssh_connection_info

DEFAULT_KEY_PATH = str(Path.home() / ".ssh" / "id_ed25519")
SSH_OPTS = [
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=15",
    "-o", "BatchMode=yes",  # никогда не спрашивать пароль/подтверждения интерактивно -
                             # если ключ не подошёл, сразу ошибка, а не зависание в ожидании ввода
]


@dataclass
class Connection:
    """Просто 'пакет' с параметрами подключения - никакой сетевой
    активности при создании, поэтому создаётся мгновенно. Реальное
    подключение происходит только в момент run_command/upload/download."""
    ip: str
    port: int
    key_path: str = DEFAULT_KEY_PATH


def connect(ip: str, port: int, key_path: str = DEFAULT_KEY_PATH) -> Connection:
    """Просто создаёт объект с параметрами - без сетевого обращения."""
    return Connection(ip=ip, port=port, key_path=key_path)


def run_command(client: Connection, command: str, on_output=None) -> int:
    """Выполняет команду на сервере через системный ssh.exe, стримит
    вывод построчно в реальном времени. Возвращает код завершения."""
    ssh_cmd = (
        ["ssh"] + SSH_OPTS +
        ["-p", str(client.port), "-i", client.key_path, f"root@{client.ip}", command]
    )

    process = subprocess.Popen(
        ssh_cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace",
    )
    _register_process(process)

    try:
        for line in process.stdout:
            line = line.rstrip("\n")
            if on_output:
                on_output(line)
            else:
                print(line)
    finally:
        _unregister_process(process)

    process.wait()
    return process.returncode


_active_processes = []
_active_processes_lock = threading.Lock()


def _register_process(proc):
    with _active_processes_lock:
        _active_processes.append(proc)


def _unregister_process(proc):
    with _active_processes_lock:
        if proc in _active_processes:
            _active_processes.remove(proc)


def cancel_all_local_transfers():
    """Принудительно останавливает все текущие локальные операции
    (scp-загрузку/скачивание файлов), запущенные из этой программы.
    Возвращает количество остановленных процессов."""
    with _active_processes_lock:
        procs = list(_active_processes)
    count = 0
    for proc in procs:
        try:
            proc.terminate()
            count += 1
        except Exception:
            pass
    return count


def upload_file(client: Connection, local_path: str, remote_path: str, on_progress=None):
    """Загружает файл на сервер через scp (можно прервать через cancel_all_local_transfers)."""
    scp_cmd = (
        ["scp"] + SSH_OPTS +
        ["-P", str(client.port), "-i", client.key_path, str(local_path), f"root@{client.ip}:{remote_path}"]
    )
    proc = subprocess.Popen(scp_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, text=True)
    _register_process(proc)
    try:
        output, _ = proc.communicate(timeout=300)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError("Загрузка не уложилась в отведённое время (5 минут)")
    finally:
        _unregister_process(proc)
    if proc.returncode != 0:
        if proc.returncode < 0:
            raise RuntimeError("Загрузка остановлена пользователем")
        raise RuntimeError(f"Ошибка scp: {output.strip()}")
    return os.path.getsize(local_path)


def get_remote_file_size(client: Connection, remote_path: str) -> int:
    """Возвращает точный размер файла на сервере в байтах (через stat)."""
    lines = []
    exit_code = run_command(client, f'stat -c%s "{remote_path}"', on_output=lines.append)
    if exit_code != 0 or not lines:
        raise RuntimeError(f"Не удалось узнать размер файла на сервере: {remote_path}")
    try:
        return int(lines[0].strip())
    except ValueError:
        raise RuntimeError(f"Не удалось разобрать размер файла на сервере: {lines[0]!r}")


def _run_sftp_reget(client: Connection, remote_path: str, local_path: str,
                     on_progress=None, expected_size: int = None, poll_interval: float = 2.0):
    """Запускает одну попытку докачки файла через sftp reget (докачивает
    с того места, где локальный файл обрывается - если файла ещё нет,
    начинает с нуля). Без жёсткого таймаута - большие архивы могут идти
    долго; отменить можно через cancel_all_local_transfers().

    Пока идёт передача, каждые poll_interval секунд проверяет текущий
    размер локального файла и вызывает on_progress(percent, transferred,
    total) - чтобы в журнале было видно движение, а не тишина до самого
    конца. Формат совпадает с тем, что уже ожидает app.py."""
    remote_dir = os.path.dirname(remote_path).replace("\\", "/") or "."
    remote_name = os.path.basename(remote_path)
    local_name = str(local_path)

    batch_commands = f'lcd "{os.path.dirname(local_name) or "."}"\ncd "{remote_dir}"\nreget "{remote_name}" "{local_name}"\nbye\n'

    with tempfile.NamedTemporaryFile(mode="w", suffix=".sftp_batch", delete=False, encoding="utf-8") as bf:
        bf.write(batch_commands)
        batch_path = bf.name

    # Вывод sftp пишем во временный файл, а не в PIPE: у sftp свой
    # прогресс-бар с частыми обновлениями, и если его не вычитывать в
    # реальном времени, буфер PIPE (обычно 64 КБ) переполнится и сам
    # процесс sftp встанет намертво в ожидании, пока кто-то его вычитает.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as out_f:
        try:
            sftp_cmd = (
                ["sftp"] + SSH_OPTS +
                ["-P", str(client.port), "-i", client.key_path, "-b", batch_path, f"root@{client.ip}"]
            )
            proc = subprocess.Popen(sftp_cmd, stdout=out_f, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL)
            _register_process(proc)
            try:
                while proc.poll() is None:
                    if on_progress:
                        current = os.path.getsize(local_name) if os.path.exists(local_name) else 0
                        total = expected_size if expected_size else current
                        percent = int(current * 100 / total) if total else 0
                        on_progress(percent, current, total)
                    time.sleep(poll_interval)
            finally:
                _unregister_process(proc)
            out_f.seek(0)
            output = out_f.read()
            return proc.returncode, output
        finally:
            try:
                os.unlink(batch_path)
            except OSError:
                pass


def download_file(client: Connection, remote_path: str, local_path: str, on_progress=None,
                   max_attempts: int = 5):
    """Скачивает файл с сервера через sftp reget (докачивает с места
    обрыва вместо того, чтобы начинать заново) и проверяет, что итоговый
    размер совпадает с размером на сервере - если нет, повторяет попытку
    (до max_attempts раз). Можно прервать через cancel_all_local_transfers().

    on_progress, если задан, вызывается как on_progress(percent, transferred,
    total) каждые ~2 секунды во время передачи - это уже тот же формат,
    который используют существующие вызовы в app.py."""
    Path(local_path).parent.mkdir(parents=True, exist_ok=True)

    expected_size = get_remote_file_size(client, remote_path)

    last_error = ""
    for attempt in range(1, max_attempts + 1):
        returncode, output = _run_sftp_reget(
            client, remote_path, local_path,
            on_progress=on_progress, expected_size=expected_size,
        )

        if returncode < 0:
            raise RuntimeError("Скачивание остановлено пользователем")

        actual_size = os.path.getsize(local_path) if os.path.exists(local_path) else 0

        if returncode == 0 and actual_size == expected_size:
            return actual_size

        last_error = output.strip() if returncode != 0 else (
            f"размер не совпал: скачано {actual_size:,}, на сервере {expected_size:,}"
        )
        # переходим к следующей попытке - reget сам продолжит с текущего места

    raise RuntimeError(
        f"Не удалось докачать файл за {max_attempts} попыток(и). "
        f"Последняя ошибка: {last_error}"
    )


def upload_directory(client: Connection, local_dir: str, remote_dir: str, on_output=None):
    """Загружает все файлы из локальной папки в папку на сервере (создаёт
    папку на сервере, если её ещё нет). Не рекурсивно."""
    run_command(client, f'mkdir -p "{remote_dir}"')

    local_path = Path(local_dir)
    files = [f for f in local_path.iterdir() if f.is_file()]
    for i, f in enumerate(files, 1):
        if on_output:
            on_output(f"  [{i}/{len(files)}] {f.name}...")
        upload_file(client, str(f), f"{remote_dir}/{f.name}")
    return len(files)


def _list_remote(client: Connection, remote_path: str):
    """Возвращает список (имя, is_dir) для содержимого папки на сервере,
    через 'ls -la' (парсинг вывода)."""
    lines = []
    exit_code = run_command(client, f'ls -la "{remote_path}"', on_output=lines.append)
    if exit_code != 0:
        raise RuntimeError(f"Не удалось прочитать папку {remote_path} (код {exit_code})")

    entries = []
    for line in lines:
        parts = line.split(None, 8)
        if len(parts) < 9:
            continue
        perms, name = parts[0], parts[8]
        if name in (".", ".."):
            continue
        is_dir = perms.startswith("d")
        entries.append((name, is_dir))
    return entries


def list_remote_dirs(client: Connection, remote_path: str):
    """Возвращает список названий подпапок внутри указанной папки на сервере."""
    entries = _list_remote(client, remote_path)
    return sorted(name for name, is_dir in entries if is_dir)


def list_remote_files(client: Connection, remote_path: str, extension: str = None):
    """Возвращает список названий файлов (не папок) внутри указанной папки
    на сервере. Если задан extension - только файлы с этим расширением."""
    entries = _list_remote(client, remote_path)
    files = [name for name, is_dir in entries if not is_dir]
    if extension:
        files = [f for f in files if f.endswith(extension)]
    return sorted(files)


def download_matching_files(client: Connection, remote_dir: str, local_dir: str,
                             extension: str = ".csv", on_output=None):
    """Скачивает все файлы с указанным расширением из папки на сервере."""
    matching = list_remote_files(client, remote_dir, extension)
    os.makedirs(local_dir, exist_ok=True)
    for i, filename in enumerate(matching, 1):
        if on_output:
            on_output(f"  [{i}/{len(matching)}] {filename}...")
        download_file(client, f"{remote_dir}/{filename}", os.path.join(local_dir, filename))
    return matching


def get_pod_ssh_connection(api_key: str):
    """Находит актуальный под и возвращает (ip, port) для прямого подключения."""
    pod = get_latest_pod(api_key)
    ip, port = get_ssh_connection_info(pod)
    if not ip:
        raise RuntimeError(
            "Не удалось получить прямой SSH-адрес - возможно, под сейчас "
            "остановлен. Сначала запусти его через runpod_controller.py start"
        )
    return ip, port


def main():
    api_key = os.environ.get("RUNPOD_API_KEY")
    if not api_key:
        print("ОШИБКА: не задан RUNPOD_API_KEY")
        sys.exit(1)

    if len(sys.argv) < 2:
        print('Использование: python ssh_runner.py "команда для выполнения"')
        sys.exit(1)

    command = sys.argv[1]

    print("Нахожу актуальный под...")
    ip, port = get_pod_ssh_connection(api_key)
    print(f"Подключаюсь к root@{ip}:{port}...")

    client = connect(ip, port)
    exit_code = run_command(client, command)

    print(f"\nКоманда завершена с кодом: {exit_code}")


if __name__ == "__main__":
    main()
