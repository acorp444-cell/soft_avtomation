"""
Показывает список твоих шаблонов озвучки в Lumean с их template_id.

ИСПОЛЬЗОВАНИЕ:
    python list_templates.py

Перед запуском:
    export LUMEAN_API_KEY="твой_ключ"   (Linux/сервер)
    set LUMEAN_API_KEY=твой_ключ        (Windows)
"""

import os
import sys
import urllib.request
import json

api_key = os.environ.get("LUMEAN_API_KEY")
if not api_key:
    print("ОШИБКА: не задан LUMEAN_API_KEY")
    sys.exit(1)

url = "https://api.lumean.app/api/public/templates"
req = urllib.request.Request(url, headers={"X-API-KEY": api_key})

with urllib.request.urlopen(req) as resp:
    data = json.loads(resp.read().decode("utf-8"))

templates = data.get("data", [])
if not templates:
    print("Шаблонов не найдено. Возможно, нужно создать через веб-кабинет или бота.")
else:
    print(f"Найдено шаблонов: {len(templates)}\n")
    for t in templates:
        config = t.get("config", {})
        voice_id = config.get("tts_settings", {}).get("voice_id", "?")
        print(f"Имя: {t['name']}")
        print(f"  template_id: {t['id']}")
        print(f"  service_key: {t['service_key']}")
        print(f"  voice_id: {voice_id}")
        print()
