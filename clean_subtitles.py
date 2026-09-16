"""
Очистка текста субтитров, скопированных вручную с YouTube.

Автоматические субтитры YouTube часто без знаков препинания, с ошибками
и иногда с повторами слов (из-за того, как субтитры "бегут" по экрану).
Этот скрипт прогоняет такой черновой текст через OpenAI и возвращает
исправленный сплошной текст (без заголовков/абзацев/markdown), с
расставленной пунктуацией и исправленными ошибками.

Запускается на RunPod (через SSH из app.py) - у OpenAI есть ограничения
по стране/региону, и прямые запросы с домашнего интернета пользователя
иногда отклоняются ("unsupported_country_region_territory"), тогда как
с сервера RunPod (другая страна) запросы проходят нормально - так же,
как и для остальных функций, использующих OpenAI (CSV, превью, музыка).

Запрос идёт через curl (а не через встроенный в Python механизм HTTPS-
запросов) - та же причина, по которой на curl переведён
royaltechno_generate.py: на части компьютеров (Windows) Python-
подключение может обрываться некоторыми сайтами, тогда как curl
проходит без проблем.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

DEFAULT_API_BASE = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-5.1"
# Большой текст субтитров (тысячи слов) даёт такой же большой ответ на
# выходе - модели требуется время, чтобы его сгенерировать. 3 минут может
# не хватить для действительно длинных текстов, поэтому запас побольше.
REQUEST_TIMEOUT_SEC = 600

TIMESTAMP_LINE_RE = re.compile(r'^\s*\d{1,2}(:\d{2}){1,2}\s*$')

SYSTEM_PROMPT = (
    "Ты - редактор-корректор. Тебе дают черновой текст, скопированный из "
    "автоматических субтитров YouTube: там могут быть отсутствующие знаки "
    "препинания, грамматические и орфографические ошибки, случайные "
    "повторы слов или обрывки фраз (из-за того, как YouTube генерирует "
    "субтитры построчно). Также в тексте могли остаться номера таймкодов "
    "(например \"0:05\", \"12:34\") - их нужно полностью убрать.\n\n"
    "Твоя задача - вернуть ИСПРАВЛЕННЫЙ текст:\n"
    "- сплошным текстом, одним потоком (без заголовков, без списков, без "
    "markdown-разметки, без пустых строк между абзацами)\n"
    "- с правильными знаками препинания\n"
    "- с исправленными грамматическими и синтаксическими ошибками\n"
    "- без таймкодов и любых технических пометок\n"
    "- без повторяющихся слов/фраз, возникших из-за особенностей автосубтитров\n\n"
    "ВАЖНО: не меняй смысл, не добавляй ничего от себя, не сокращай "
    "содержание - только исправляй ошибки и восстанавливай знаки "
    "препинания. Ответь ТОЛЬКО исправленным текстом, без пояснений до "
    "или после."
)


def strip_timestamp_lines(raw_text):
    """Убирает строки-таймкоды (например "0:05"), которые YouTube
    добавляет при копировании транскрипта - экономит токены и не путает
    модель посторонними числами."""
    lines = [ln for ln in raw_text.splitlines() if not TIMESTAMP_LINE_RE.match(ln)]
    return "\n".join(lines)


def _run_curl(cmd, timeout_sec):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec + 10)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"OpenAI не ответил за {timeout_sec} сек") from e
    except FileNotFoundError as e:
        raise RuntimeError(
            "Не найдена программа 'curl' на этой машине - проверь командой "
            "'curl --version'") from e


def clean_subtitles_text(raw_text, api_key, model=DEFAULT_MODEL, api_base=DEFAULT_API_BASE, log=print):
    cleaned_input = strip_timestamp_lines(raw_text).strip()
    if not cleaned_input:
        raise ValueError("Пустой текст - нечего исправлять")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": cleaned_input},
        ],
        "temperature": 0.2,
    }

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            json.dump(payload, tmp)
            tmp_path = tmp.name

        log(f"[i] Отправляю текст в OpenAI ({model}, {len(cleaned_input)} символов)...")
        cmd = ["curl", "-s", "-S", "-X", "POST", f"{api_base}/chat/completions",
               "--max-time", str(REQUEST_TIMEOUT_SEC),
               "-H", f"Authorization: Bearer {api_key}",
               "-H", "Content-Type: application/json",
               "-d", f"@{tmp_path}",
               "-w", "\n%{http_code}"]
        result = _run_curl(cmd, REQUEST_TIMEOUT_SEC)
    finally:
        if tmp_path:
            os.unlink(tmp_path)

    if result.returncode != 0:
        raise RuntimeError(f"curl не смог связаться с OpenAI (код {result.returncode}): "
                            f"{result.stderr.strip()[-500:]}")

    body, _, status_code = result.stdout.rpartition("\n")
    try:
        status = int(status_code)
    except ValueError:
        raise RuntimeError(f"Не удалось разобрать ответ OpenAI: {result.stdout[:500]}")

    if status >= 400:
        raise RuntimeError(f"Ошибка OpenAI API {status}: {body}")

    data = json.loads(body)
    content = data["choices"][0]["message"]["content"].strip()
    log("[+] Готово.")
    return content


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Файл с черновым текстом субтитров")
    parser.add_argument("--output", required=True, help="Куда сохранить исправленный текст")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--api-key", default=None, help="Ключ OpenAI (или переменная окружения OPENAI_API_KEY)")
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("ОШИБКА: не задан ключ OpenAI (--api-key или переменная OPENAI_API_KEY)")
        sys.exit(1)

    with open(args.input, "r", encoding="utf-8") as f:
        raw_text = f.read()

    cleaned = clean_subtitles_text(raw_text, api_key, model=args.model, api_base=args.api_base)

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(cleaned)
    print(f"Готово! Сохранено: {args.output}")


if __name__ == "__main__":
    main()
