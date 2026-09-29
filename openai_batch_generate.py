"""
Генерация обычных (не инфографика) картинок через OpenAI Batch API -
запасной/экономный вариант рядом с RoyalTechno.

Работает ТОЛЬКО на RunPod (как и generate_infographic_images_openai.py) -
OpenAI заблокирован по региону с домашнего интернета пользователя.

Batch-режим вдвое дешевле обычного API, но не мгновенный - OpenAI
обрабатывает пачку запросов в фоне (обычно быстрее, но гарантия - до
24 часов). Поэтому это ДВЕ отдельные команды, а не одна:

  1. submit - собирает промты по CSV, отправляет пачку в OpenAI, тут же
     возвращает управление (RunPod можно сразу выключать - ждать
     готовности не нужно).
  2. check  - проверяет, готова ли пачка. Если ещё нет - просто
     показывает статус и ничего не делает (можно выключить RunPod и
     проверить попозже). Если готова - скачивает и сохраняет все
     картинки.

Результат сохраняется СРАЗУ в папку ComfyUI/input на RunPod (не на
компьютер пользователя) - через OpenAI и так приходится идти через
RunPod, поэтому нет смысла ещё и гонять файлы обратно на компьютер и
заливать заново для апскейла (шаг B). После check можно сразу нажимать
кнопку B.

ИСПОЛЬЗОВАНИЕ:
    python3 openai_batch_generate.py submit --csv результаты/4_блок.csv \
        --library OBJECT_LIBRARY.md --output-dir /workspace/runpod-slim/ComfyUI/input \
        --quality low

    python3 openai_batch_generate.py check --output-dir /workspace/runpod-slim/ComfyUI/input
"""

import argparse
import base64
import io
import json
import os
import sys
import tempfile
from pathlib import Path

try:
    from openai import OpenAI
except ImportError:
    print("Не установлена библиотека openai. Выполните: pip install openai")
    sys.exit(1)

try:
    from PIL import Image
except ImportError:
    print("Не установлена библиотека Pillow. Выполните: pip install Pillow")
    sys.exit(1)

from generate_via_api_and_upscale import parse_library, expand_tags, read_rows

MODEL = "gpt-image-2"
SIZE = "1536x1024"  # ближайший к 16:9 из того, что вообще даёт OpenAI (см. generate_infographic_images_openai.py)
CROP_WIDTH, CROP_HEIGHT = 1536, 864  # обрезка до ровно 16:9
DEFAULT_QUALITY = "low"  # для массовой генерации обычных кадров - разница с medium/high в цене большая, а не в критичном месте (текста тут нет)

STATE_FILENAME = "openai_batch_state.json"

sys.stdout.reconfigure(line_buffering=True)


def _get_client():
    openai_key = os.environ.get("OPENAI_API_KEY")
    openai_base_url = os.environ.get("OPENAI_BASE_URL")
    if not openai_key:
        print("ОШИБКА: не задан OPENAI_API_KEY")
        sys.exit(1)
    return OpenAI(api_key=openai_key, base_url=openai_base_url) if openai_base_url \
        else OpenAI(api_key=openai_key)


def _crop_to_16_9(image_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(image_bytes))
    left = 0
    top = (img.height - CROP_HEIGHT) // 2
    cropped = img.crop((left, top, left + CROP_WIDTH, top + CROP_HEIGHT))
    buf = io.BytesIO()
    cropped.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def _state_path(output_dir: Path) -> Path:
    return output_dir / STATE_FILENAME


def cmd_submit(args):
    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"ОШИБКА: CSV не найден: {csv_path}")
        sys.exit(1)

    library_path = Path(args.library)
    library = parse_library(str(library_path)) if library_path.exists() else {}
    print(f"[i] Библиотека объектов: {len(library)} тегов найдено")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    state_path = _state_path(output_dir)
    if state_path.exists():
        print(f"ОШИБКА: уже есть незавершённая пачка в этой папке ({state_path.name}). "
              f"Сначала выполните 'check' для неё, или удалите файл вручную, если она не нужна.")
        sys.exit(1)

    rows = read_rows(str(csv_path))

    tasks = []  # (custom_id, prompt)
    for row in rows:
        num = (row.get("num") or "").strip()
        if not num:
            continue
        # обычные картинки - инфографика (source=AI_INFOGRAPHIC) идёт
        # отдельным скриптом (generate_infographic_images_openai.py)
        if (row.get("source") or "").strip() == "AI_INFOGRAPHIC":
            continue
        ref_tags = (row.get("ref_tags") or "").strip()
        for which, col in (("img1", "img_prompt_1"), ("img2", "img_prompt_2")):
            base_prompt = (row.get(col) or "").strip()
            if not base_prompt or base_prompt == "-":
                continue
            dest = output_dir / f"{num}_{which}_raw.jpg"
            if dest.exists():
                continue  # уже готово с прошлого раза
            full_prompt = expand_tags(base_prompt, ref_tags, library)
            custom_id = f"{num}_{which}"
            tasks.append((custom_id, full_prompt))

    if not tasks:
        print("[i] Генерировать нечего - либо CSV пуст, либо все картинки уже готовы.")
        return

    print(f"[i] Собираю пачку: {len(tasks)} картинок (модель {MODEL}, качество {args.quality})")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False, encoding="utf-8") as tmp:
        for custom_id, prompt in tasks:
            line = {
                "custom_id": custom_id,
                "method": "POST",
                "url": "/v1/images/generations",
                "body": {
                    "model": MODEL,
                    "prompt": prompt,
                    "size": SIZE,
                    "quality": args.quality,
                    "n": 1,
                },
            }
            tmp.write(json.dumps(line, ensure_ascii=False) + "\n")
        jsonl_path = tmp.name

    client = _get_client()
    try:
        with open(jsonl_path, "rb") as f:
            uploaded = client.files.create(file=f, purpose="batch")
        print(f"[+] Файл с запросами загружен, id: {uploaded.id}")

        batch = client.batches.create(
            input_file_id=uploaded.id,
            endpoint="/v1/images/generations",
            completion_window="24h",
        )
        print(f"[+] Пачка отправлена, id: {batch.id}, статус: {batch.status}")
    finally:
        os.unlink(jsonl_path)

    state = {
        "batch_id": batch.id,
        "count": len(tasks),
        "quality": args.quality,
    }
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nГотово! Пачка из {len(tasks)} картинок отправлена в OpenAI.")
    print("Можно выключать RunPod - результат не мгновенный (может занять до суток).")
    print("Когда захотите проверить - снова включите RunPod и выполните команду 'check' для этой же папки.")


def cmd_check(args):
    output_dir = Path(args.output_dir)
    state_path = _state_path(output_dir)
    if not state_path.exists():
        print(f"ОШИБКА: не найдена отправленная пачка в {output_dir} "
              f"(файл {STATE_FILENAME} отсутствует) - сначала нужно выполнить 'submit'.")
        sys.exit(1)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    batch_id = state["batch_id"]

    client = _get_client()
    batch = client.batches.retrieve(batch_id)
    print(f"[i] Пачка {batch_id}: статус - {batch.status}")

    if batch.status in ("validating", "in_progress", "finalizing"):
        counts = batch.request_counts
        if counts:
            print(f"[i] Прогресс: {counts.completed}/{counts.total} готово, {counts.failed} ошибок")
        print("[i] Ещё не готово - попробуйте проверить позже (RunPod пока можно выключить).")
        return

    if batch.status in ("failed", "expired", "cancelled"):
        print(f"[!!!] Пачка завершилась со статусом '{batch.status}' - результата не будет.")
        state_path.unlink()
        return

    if batch.status != "completed":
        print(f"[!] Неожиданный статус: {batch.status}")
        return

    done, failed = 0, 0

    if batch.output_file_id:
        content = client.files.content(batch.output_file_id)
        for line in content.text.splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            custom_id = entry.get("custom_id", "?")
            response = entry.get("response") or {}
            body = response.get("body") or {}
            data = body.get("data") or []
            if not data or not data[0].get("b64_json"):
                print(f"  [!!!] {custom_id}: в ответе нет картинки")
                failed += 1
                continue
            try:
                raw_bytes = base64.b64decode(data[0]["b64_json"])
                dest = output_dir / f"{custom_id}_raw.jpg"
                dest.write_bytes(_crop_to_16_9(raw_bytes))
                print(f"  [+] {custom_id}: сохранено ({dest.name})")
                done += 1
            except Exception as e:
                print(f"  [!!!] {custom_id}: не удалось сохранить - {e}")
                failed += 1

    if batch.error_file_id:
        error_content = client.files.content(batch.error_file_id)
        for line in error_content.text.splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            custom_id = entry.get("custom_id", "?")
            error = entry.get("error") or entry.get("response", {}).get("body", {}).get("error", {})
            print(f"  [!!!] {custom_id}: ошибка от OpenAI - {error}")
            failed += 1

    print(f"\nГотово! Сохранено картинок: {done}, ошибок: {failed}")
    state_path.unlink()


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    p_submit = sub.add_parser("submit", help="Отправить пачку запросов в OpenAI")
    p_submit.add_argument("--csv", required=True)
    p_submit.add_argument("--library", default="OBJECT_LIBRARY.md")
    p_submit.add_argument("--output-dir", required=True)
    p_submit.add_argument("--quality", default=DEFAULT_QUALITY, choices=["low", "medium", "high"])
    p_submit.set_defaults(func=cmd_submit)

    p_check = sub.add_parser("check", help="Проверить и скачать готовую пачку")
    p_check.add_argument("--output-dir", required=True)
    p_check.set_defaults(func=cmd_check)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
