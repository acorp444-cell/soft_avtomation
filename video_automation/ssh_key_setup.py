"""
Автоматически прописывает публичный SSH-ключ в ~/.ssh/authorized_keys
на поде через управляемый прокси ssh.runpod.io (он всегда аутентифицирует
по ключу аккаунта, в отличие от прямого IP-подключения).

Нужно вызывать один раз после каждого старта пода (в том числе после
миграции) - тогда прямое подключение по IP дальше будет работать без
пароля, независимо от того, что физический контейнер каждый раз новый.

ТРЕБОВАНИЯ: системный ssh.exe (есть по умолчанию на Windows 10/11)
"""

import subprocess
from pathlib import Path

from runpod_controller import get_ssh_username

DEFAULT_KEY_PATH = str(Path.home() / ".ssh" / "id_ed25519")
DEFAULT_PUBKEY_PATH = str(Path.home() / ".ssh" / "id_ed25519.pub")


def ensure_key_installed(pod: dict, key_path: str = DEFAULT_KEY_PATH,
                          pubkey_path: str = DEFAULT_PUBKEY_PATH, on_output=None) -> bool:
    """Прописывает публичный ключ в authorized_keys на поде через прокси.
    Возвращает True при успехе."""
    username_at_host = get_ssh_username(pod)
    if not username_at_host:
        if on_output:
            on_output("Не удалось получить адрес прокси для установки ключа")
        return False

    pubkey_content = Path(pubkey_path).read_text(encoding="utf-8").strip()

    setup_commands = (
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh\n"
        f'echo "{pubkey_content}" >> ~/.ssh/authorized_keys\n'
        "sort -u -o ~/.ssh/authorized_keys ~/.ssh/authorized_keys\n"  # убираем дубликаты, если ключ уже был
        "chmod 600 ~/.ssh/authorized_keys\n"
        "echo KEY_SETUP_DONE\n"
        "exit\n"
    )

    ssh_cmd = [
        "ssh", "-t", "-t",
        "-o", "StrictHostKeyChecking=accept-new",
        "-i", key_path,
        f"{username_at_host}@ssh.runpod.io",
    ]

    process = subprocess.Popen(
        ssh_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace",
    )
    process.stdin.write(setup_commands)
    process.stdin.flush()
    process.stdin.close()

    success = False
    for line in process.stdout:
        line = line.rstrip("\n").rstrip("\r")
        if "KEY_SETUP_DONE" in line:
            success = True
        if on_output:
            on_output(line)

    process.wait()
    return success
