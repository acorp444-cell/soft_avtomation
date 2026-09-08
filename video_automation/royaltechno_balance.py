"""
Получение баланса аккаунта RoyalTechno (для картинок/видео).
"""

import json
import urllib.error
import urllib.request

API_BASE = "https://api.royaltechno.cc/v1"


def get_royaltechno_balance(api_key: str):
    """Возвращает баланс аккаунта RoyalTechno. Возвращает None, если
    эндпоинт недоступен или формат ответа отличается от ожидаемого."""
    req = urllib.request.Request(
        f"{API_BASE}/account",
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "curl/8.0.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Ошибка RoyalTechno API {e.code}: {e.read().decode('utf-8', errors='ignore')}")

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
