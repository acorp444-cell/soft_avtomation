"""
Модуль для выполнения команд на поде RunPod и передачи файлов.

ВАЖНОЕ ИЗМЕНЕНИЕ #1: раньше использовалась библиотека paramiko, но она
периодически "зависала" на некоторых серверах без вывода ошибки - хотя
тот же самый системный ssh.exe подключался мгновенно. Поэтому весь
модуль использует СИСТЕМНЫЙ ssh.exe (та же самая программа, которой ты
подключаешься вручную) - надёжнее.

ВАЖНОЕ ИЗМЕНЕНИЕ #2: часть подов RunPod (особенно новые/пересозданные)
больше не дают прямой IP-адрес для SSH вообще - только доступ через
управляемый прокси ssh.runpod.io, а он НЕ поддерживает SCP/SFTP (так
и написано в интерфейсе RunPod). Поэтому передача файлов теперь идёт
не через scp/sftp, а через обычный `ssh ... "cat файл"` (для скачивания)
и `ssh ... "cat > файл"` (для загрузки) - это работает одинаково и при
прямом IP, и через прокси, потому что это просто выполнение команды.

ИСПОЛЬЗОВАНИЕ (для проверки):
    set RUNPOD_API_KEY=твой_ключ
    python ssh_runner.py "echo hello from server"

ТРЕБОВАНИЯ: системный ssh.exe (есть по умолчанию на Windows 10/11).
"""

import os
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from runpod_controller import get_latest_pod, get_ssh_connection_info, get_ssh_username

DEFAULT_KEY_PATH = str(Path.home() / ".ssh" / "id_ed25519")
SSH_OPTS = [
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=15",
    "-o", "BatchMode=yes",  # никогда не спрашивать пароль/подтверждения интерактивно -
                             # если ключ не подошёл, сразу ошибка, а не зависание в ожидании ввода
]
TRANSFER_CHUNK_SIZE = 1024 * 1024  # 1 МБ - для прогресса передачи файлов


@dataclass
class Connection:
    """Просто 'пакет' с параметрами подключения - никакой сетевой
    активности при создании, поэтому создаётся мгновенно. Реальное
    подключение происходит только в момент run_command/upload/download.

    Ровно один из двух способов адресации:
    - ip+port - прямое подключение (есть не у всех подов)
    - proxy_user - через управляемый прокси ssh.runpod.io (есть всегда,
      но сам по себе не поддерживает SCP/SFTP - см. transfer-функции
      ниже, которые поэтому не используют scp/sftp вообще)."""
    ip: str = None
    port: int = None
    proxy_user: str = None
    key_path: str = DEFAULT_KEY_PATH

    def ssh_target_args(self):
        """Аргументы для ssh - куда и с каким ключом подключаться,
        независимо от того, прямой это адрес или прокси. Прокси
        ssh.runpod.io требует псевдотерминал (без -t -t отвечает
        "Your SSH client doesn't support PTY") - прямому подключению
        это не нужно и не запрашивается, чтобы не менять его поведение."""
        if self.proxy_user:
            return ["-t", "-t", "-i", self.key_path, f"{self.proxy_user}@ssh.runpod.io"]
        return ["-p", str(self.port), "-i", self.key_path, f"root@{self.ip}"]

    def wrap_remote_command(self, command: str) -> str:
        """Оборачивает команду для выполнения через псевдотерминал (прокси) -
        отключает локальное эхо и преобразование переносов строк (stty
        raw), которые иначе искажают бинарные данные (картинки, видео)
        при передаче файлов через такое подключение. Для прямого
        подключения (без псевдотерминала) ничего не меняет."""
        if self.proxy_user:
            return f"stty raw -echo 2>/dev/null; {command}"
        return command


def connect(ip: str, port: int, key_path: str = DEFAULT_KEY_PATH) -> Connection:
    """Просто создаёт объект с параметрами - без сетевого обращения."""
    return Connection(ip=ip, port=port, key_path=key_path)


def connect_proxy(proxy_user: str, key_path: str = DEFAULT_KEY_PATH) -> Connection:
    """То же самое, но через управляемый прокси ssh.runpod.io - для
    подов без прямого IP."""
    return Connection(proxy_user=proxy_user, key_path=key_path)


def get_connection_for_pod(pod: dict, key_path: str = DEFAULT_KEY_PATH) -> Connection:
    """Строит подключение для конкретного пода - прямое, если доступно,
    иначе через прокси. Бросает ошибку, только если недоступно вообще
    ничего (под ещё не готов)."""
    ip, port = get_ssh_connection_info(pod)
    if ip and port:
        return connect(ip, port, key_path)
    proxy_user = get_ssh_username(pod)
    if proxy_user:
        return connect_proxy(proxy_user, key_path)
    raise RuntimeError(
        "Не удалось получить SSH-адрес (ни прямой, ни через прокси) - "
        "возможно, под сейчас остановлен. Сначала запусти его через "
        "runpod_controller.py start"
    )


def run_command(client: Connection, command: str, on_output=None) -> int:
    """Выполняет команду на сервере через системный ssh.exe, стримит
    вывод построчно в реальном времени. Возвращает код завершения."""
    ssh_cmd = ["ssh"] + SSH_OPTS + client.ssh_target_args() + [client.wrap_remote_command(command)]

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
    """Загружает файл на сервер через ssh + cat (вместо scp - scp не
    поддерживает подключение через управляемый прокси ssh.runpod.io,
    которое есть у части подов, а обычный ssh с ним работает как
    обычно). Можно прервать через cancel_all_local_transfers()."""
    total_size = os.path.getsize(local_path)
    cmd = ["ssh"] + SSH_OPTS + client.ssh_target_args() + [client.wrap_remote_command(f'cat > "{remote_path}"')]

    # вывод пишем во временный файл, а не в PIPE - иначе если удалённая
    # сторона вдруг что-то напишет в stdout/stderr, пока мы ещё пишем в
    # stdin, буфер PIPE может переполниться и всё зависнет намертво
    with tempfile.TemporaryFile(mode="w+b") as err_f:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=err_f, stderr=subprocess.STDOUT)
        _register_process(proc)
        try:
            sent = 0
            with open(local_path, "rb") as f:
                while True:
                    chunk = f.read(TRANSFER_CHUNK_SIZE)
                    if not chunk:
                        break
                    try:
                        proc.stdin.write(chunk)
                    except (BrokenPipeError, OSError):
                        break
                    sent += len(chunk)
                    if on_progress:
                        percent = int(sent * 100 / total_size) if total_size else 100
                        on_progress(percent, sent, total_size)
            try:
                proc.stdin.close()
            except OSError:
                pass
            proc.wait()
        finally:
            _unregister_process(proc)

        if proc.returncode != 0:
            if proc.returncode < 0:
                raise RuntimeError("Загрузка остановлена пользователем")
            err_f.seek(0)
            error_text = err_f.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Ошибка загрузки: {error_text}")
    return total_size


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


def _download_attempt(client: Connection, remote_path: str, local_path: str,
                       expected_size: int, on_progress=None) -> int:
    """Одна попытка скачивания через ssh + cat/tail (вместо sftp reget -
    sftp не поддерживает управляемый прокси ssh.runpod.io). Если локальный
    файл уже частично скачан с прошлой попытки - докачивает остаток
    (tail -c +N на сервере), а не начинает заново. Возвращает итоговый
    размер локального файла после этой попытки."""
    offset = os.path.getsize(local_path) if os.path.exists(local_path) else 0
    if offset > expected_size:
        os.remove(local_path)  # похоже, локальный файл от чего-то другого - лучше начать заново
        offset = 0
    elif offset == expected_size:
        return offset

    remote_cmd = f'cat "{remote_path}"' if offset == 0 else f'tail -c +{offset + 1} "{remote_path}"'
    cmd = ["ssh"] + SSH_OPTS + client.ssh_target_args() + [client.wrap_remote_command(remote_cmd)]
    mode = "wb" if offset == 0 else "ab"

    with tempfile.TemporaryFile(mode="w+b") as err_f, open(local_path, mode) as out_f:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err_f, stdin=subprocess.DEVNULL)
        _register_process(proc)
        try:
            transferred = offset
            while True:
                chunk = proc.stdout.read(TRANSFER_CHUNK_SIZE)
                if not chunk:
                    break
                out_f.write(chunk)
                transferred += len(chunk)
                if on_progress:
                    percent = int(transferred * 100 / expected_size) if expected_size else 100
                    on_progress(percent, transferred, expected_size)
            proc.wait()
        finally:
            _unregister_process(proc)

        if proc.returncode != 0:
            if proc.returncode < 0:
                raise RuntimeError("Скачивание остановлено пользователем")
            err_f.seek(0)
            error_text = err_f.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Ошибка скачивания: {error_text}")

    return os.path.getsize(local_path)


def download_file(client: Connection, remote_path: str, local_path: str, on_progress=None,
                   max_attempts: int = 5):
    """Скачивает файл с сервера (докачивает с места обрыва вместо того,
    чтобы начинать заново) и проверяет, что итоговый размер совпадает с
    размером на сервере - если нет, повторяет попытку (до max_attempts
    раз). Можно прервать через cancel_all_local_transfers().

    on_progress, если задан, вызывается как on_progress(percent, transferred,
    total) по мере передачи - тот же формат, что уже ожидает app.py."""
    Path(local_path).parent.mkdir(parents=True, exist_ok=True)

    expected_size = get_remote_file_size(client, remote_path)

    last_error = ""
    for attempt in range(1, max_attempts + 1):
        try:
            actual_size = _download_attempt(client, remote_path, local_path, expected_size, on_progress)
        except RuntimeError as e:
            if "остановлено пользователем" in str(e):
                raise
            last_error = str(e)
            continue

        if actual_size == expected_size:
            return actual_size

        last_error = f"размер не совпал: скачано {actual_size:,}, на сервере {expected_size:,}"
        # переходим к следующей попытке - _download_attempt сам продолжит с текущего места

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


def get_pod_ssh_connection(api_key: str) -> Connection:
    """Находит актуальный под и возвращает готовое подключение (Connection) -
    прямое, если под его поддерживает, иначе через прокси ssh.runpod.io."""
    pod = get_latest_pod(api_key)
    return get_connection_for_pod(pod)


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
    client = get_pod_ssh_connection(api_key)
    if client.proxy_user:
        print(f"Подключаюсь через прокси {client.proxy_user}@ssh.runpod.io...")
    else:
        print(f"Подключаюсь к root@{client.ip}:{client.port}...")
    exit_code = run_command(client, command)

    print(f"\nКоманда завершена с кодом: {exit_code}")


if __name__ == "__main__":
    main()
