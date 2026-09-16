"""
Получение баланса аккаунта RoyalTechno (для картинок/видео).
"""

import json
import subprocess

API_BASE = "https://api.royaltechno.cc/v1"
REQUEST_TIMEOUT_SEC = 15


def get_royaltechno_balance(api_key: str):
    """Возвращает баланс аккаунта RoyalTechno. Возвращает None, если
    эндпоинт недоступен или формат ответа отличается от ожидаемого.

    Запрос идёт через curl (а не через встроенный в Python механизм
    HTTPS-запросов) - на части компьютеров (Windows) Python-подключение
    почему-то обрывается сервером RoyalTechno (Cloudflare), хотя точно
    такой же запрос через curl проходит без проблем."""
    cmd = ["curl", "-s", "-X", "GET", f"{API_BASE}/account",
           "--max-time", str(REQUEST_TIMEOUT_SEC),
           "-H", f"Authorization: Bearer {api_key}",
           "-w", "\n%{http_code}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=REQUEST_TIMEOUT_SEC + 10)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"RoyalTechno не ответил за {REQUEST_TIMEOUT_SEC} сек") from e
    except FileNotFoundError as e:
        raise RuntimeError(
            "Не найдена программа 'curl' на этом компьютере (обычно она уже "
            "встроена в Windows 10/11)") from e

    if result.returncode != 0:
        raise RuntimeError(f"curl не смог связаться с RoyalTechno (код {result.returncode}): "
                            f"{result.stderr.strip()[-500:]}")

    body, _, status_code = result.stdout.rpartition("\n")
    try:
        status = int(status_code)
    except ValueError:
        raise RuntimeError(f"Не удалось разобрать ответ RoyalTechno: {result.stdout[:500]}")

    if status >= 400:
        raise RuntimeError(f"Ошибка RoyalTechno API {status}: {body}")

    data = json.loads(body)

    # Пробуем несколько вероятных названий поля с балансом
    for key in ("balance", "balance_usd", "credit", "credits", "balance_usd_cents"):
        if key in data:
            value = data[key]
            if isinstance(value, str):
                return value  # уже отформатировано сервисом (например "$8.00")
            if "cents" in key:
                value = value / 100
            return value

    # Не нашли ожидаемое поле - возвращаем весь ответ для ручной проверки
    return data
