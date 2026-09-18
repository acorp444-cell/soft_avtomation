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
import os
import sys
from pathlib import Path

try:
    from openai import OpenAI
except ImportError:
    print("Не установлена библиотека openai. Выполните: pip install openai")
    sys.exit(1)

MODEL = "gpt-image-1"
SIZE = "1024x1024"
CSV_DELIMITER = ";"

# то же самое, что и построчный вывод без буферизации в остальных
# скриптах - иначе журнал в приложении на компьютере "молчит" минутами
sys.stdout.reconfigure(line_buffering=True)


def generate_and_save(client, prompt: str, dest_path: Path) -> bool:
    try:
        response = client.images.generate(model=MODEL, prompt=prompt, size=SIZE, n=1)
    except Exception as e:
        print(f"  [!!!] Ошибка запроса к OpenAI: {e}")
        return False

    b64 = response.data[0].b64_json
    if not b64:
        print("  [!!!] OpenAI не вернул картинку")
        return False

    try:
        dest_path.write_bytes(base64.b64decode(b64))
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
