"""
Генерирует картинки-инфографику через OpenAI (gpt-image-1) для строк
CSV с source=AI_INFOGRAPHIC.

Зачем отдельный путь: RoyalTechno (nano-banana) заметно хуже рисует
читаемый текст на картинке, особенно кириллицу - буквы часто выходят
искажёнными или вообще на английском, даже если в промте явно написан
русский текст. OpenAI справляется с этим намного лучше. Обычные кадры
(без текста) по-прежнему генерируются через RoyalTechno как раньше -
этот скрипт трогает ТОЛЬКО строки с source=AI_INFOGRAPHIC.

Работает исключительно на RunPod - OpenAI заблокирован по региону с
домашнего интернета пользователя (см. остальные скрипты, где это уже
обходили тем же способом). Запускается как часть шага B (апскейл),
перед самим апскейлом - чтобы недостающие инфографические картинки
успели появиться в общей папке с сырыми картинками до того, как всё
уйдёт на апскейл вместе.

Пропускает картинки, для которых файл уже существует (можно
перезапускать сколько угодно раз, не тратя деньги повторно).

ИСПОЛЬЗОВАНИЕ:
    python3 generate_infographic_images_openai.py \
        --csv результаты/4_блок.csv \
        --output-dir /workspace/runpod-slim/ComfyUI/input
"""

import argparse
import base64
import csv
import io
import os
import sys
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

MODEL = "gpt-image-2"  # gpt-image-1 отключается OpenAI 23 октября 2026
# у OpenAI нет готового формата 16:9 - только 1024x1024 (квадрат),
# 1536x1024 (3:2) и 1024x1536 (2:3). Генерируем ближайший альбомный (3:2),
# затем обрезаем по центру до точных 16:9 - см. _crop_to_16_9()
SIZE = "1536x1024"
CROP_WIDTH, CROP_HEIGHT = 1536, 864  # 1536x864 = ровно 16:9
# инфографики немного (несколько кадров на блок), а весь смысл именно в
# ЧЁТКОМ читаемом тексте - тут не экономим на качестве, как в массовой
# batch-генерации обычных картинок
QUALITY = "medium"
CSV_DELIMITER = ";"


def _crop_to_16_9(image_bytes: bytes) -> bytes:
    """Обрезает картинку по центру с 1536x1024 (3:2, всё что даёт OpenAI)
    до 1536x864 (ровно 16:9, как у остального видео) - убирает по 80px
    сверху и снизу, по бокам не трогает."""
    img = Image.open(io.BytesIO(image_bytes))
    left = 0
    top = (img.height - CROP_HEIGHT) // 2
    cropped = img.crop((left, top, left + CROP_WIDTH, top + CROP_HEIGHT))
    buf = io.BytesIO()
    cropped.save(buf, format="JPEG", quality=95)
    return buf.getvalue()

# то же самое, что и построчный вывод без буферизации в остальных
# скриптах - иначе журнал в приложении на компьютере "молчит" минутами
sys.stdout.reconfigure(line_buffering=True)


def generate_and_save(client, prompt: str, dest_path: Path) -> bool:
    try:
        response = client.images.generate(model=MODEL, prompt=prompt, size=SIZE, quality=QUALITY, n=1)
    except Exception as e:
        print(f"  [!!!] Ошибка запроса к OpenAI: {e}")
        return False

    b64 = response.data[0].b64_json
    if not b64:
        print("  [!!!] OpenAI не вернул картинку")
        return False

    try:
        raw_bytes = base64.b64decode(b64)
        dest_path.write_bytes(_crop_to_16_9(raw_bytes))
    except Exception as e:
        print(f"  [!!!] Не удалось сохранить файл: {e}")
        return False
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True, help="CSV-файл блока")
    parser.add_argument("--output-dir", required=True,
                         help="Папка для сырых картинок (та же, куда шаг B грузит остальные)")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"ОШИБКА: CSV не найден: {csv_path}")
        sys.exit(1)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    openai_key = os.environ.get("OPENAI_API_KEY")
    openai_base_url = os.environ.get("OPENAI_BASE_URL")
    if not openai_key:
        print("ОШИБКА: не задан OPENAI_API_KEY")
        sys.exit(1)
    client = OpenAI(api_key=openai_key, base_url=openai_base_url) if openai_base_url \
        else OpenAI(api_key=openai_key)

    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=CSV_DELIMITER)
        rows = list(reader)

    targets = []
    for row in rows:
        if (row.get("source") or "").strip() != "AI_INFOGRAPHIC":
            continue
        num = (row.get("num") or "").strip()
        if not num:
            continue
        for which, prompt_field in (("img1", "img_prompt_1"), ("img2", "img_prompt_2")):
            prompt = (row.get(prompt_field) or "").strip()
            if not prompt:
                continue
            dest = output_dir / f"{num}_{which}_raw.jpg"
            if dest.exists():
                continue  # уже готово с прошлого раза - не тратим деньги заново
            targets.append((num, which, prompt, dest))

    if not targets:
        print("[i] Инфографических картинок для генерации не найдено (или уже все готовы).")
        return

    print(f"[i] Нужно сгенерировать инфографических картинок: {len(targets)}")
    done, failed = 0, 0
    for i, (num, which, prompt, dest) in enumerate(targets, 1):
        print(f"[{i}/{len(targets)}] {num}_{which} (инфографика через OpenAI)...")
        if generate_and_save(client, prompt, dest):
            print(f"  [+] Готово: {dest.name}")
            done += 1
        else:
            failed += 1

    print(f"\nГотово! Сгенерировано: {done}, ошибок: {failed}")
    if failed:
        print("[!] Для кадров с ошибками картинки не появятся - апскейл их просто пропустит, "
              "как обычно пропускает недостающие файлы.")


if __name__ == "__main__":
    main()
