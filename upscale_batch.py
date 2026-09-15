"""
Апскейл ПАЧКИ уже сгенерированных (сырых) картинок через локальный ComfyUI
на этом сервере (RunPod) - используется вместе с royaltechno_generate.py
(который генерирует сырые картинки БЕЗ участия RunPod, прямо на
компьютере пользователя).

Новая логика экономии RunPod:
  1. На компьютере: генерация картинок через RoyalTechno (без RunPod)
  2. RunPod включается коротко ТОЛЬКО для этого шага - апскейл уже
     готовых картинок (видеокарта нужна только здесь)
  3. Результат скачивается обратно на компьютер, RunPod выключается
  4. На компьютере: видео из апскейленных картинок через RoyalTechno/Veo
     (снова без RunPod)

В отличие от старого generate_via_api_and_upscale.py (где апскейл
запускался "и забыл", без ожидания завершения), этот скрипт ЖДЁТ
завершения каждого апскейла через ComfyUI (/history), чтобы к моменту
скачивания результатов на компьютер файлы были точно готовы.

ИСПОЛЬЗОВАНИЕ:
    python3 upscale_batch.py --input-dir ../input --output-dir ../output
"""

import argparse
import copy
import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COMFYUI_URL = "http://127.0.0.1:8188"
DEFAULT_INPUT_DIR = os.path.join(BASE_DIR, "..", "input")
DEFAULT_OUTPUT_DIR = os.path.join(BASE_DIR, "..", "output")
DEFAULT_WORKFLOW_PATH = os.path.join(BASE_DIR, "workflow_upscale_only.json")

FINAL_WIDTH = 2048
FINAL_HEIGHT = 1152

POLL_EVERY_SEC = 2
POLL_TIMEOUT_SEC = 180

RAW_NAME_RE = re.compile(r'^(\d+)_(img1|img2)_raw\.jpg$')

# Форсируем построчный вывод без буферизации - иначе журнал в приложении
# на компьютере "молчит" минутами, хотя скрипт реально работает (та же
# причина, что и в assemble_block_video.py).
sys.stdout.reconfigure(line_buffering=True)


def load_workflow_template(path):
    if not os.path.exists(path):
        print(f"[!] Не найден файл схемы апскейла: {path}")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def already_upscaled(output_dir, prefix):
    return bool(glob.glob(os.path.join(output_dir, f"{prefix}_*.png")))


def submit_upscale(template, input_filename, output_prefix):
    wf = copy.deepcopy(template)
    wf["1"]["inputs"]["image"] = input_filename
    wf["12"]["inputs"]["width"] = FINAL_WIDTH
    wf["12"]["inputs"]["height"] = FINAL_HEIGHT
    wf["9"]["inputs"]["filename_prefix"] = output_prefix

    payload = json.dumps({"prompt": wf}).encode("utf-8")
    req = urllib.request.Request(
        f"{COMFYUI_URL}/prompt", data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    return result["prompt_id"]


def wait_for_comfy_job(prompt_id, timeout_sec=POLL_TIMEOUT_SEC):
    started = time.time()
    while time.time() - started < timeout_sec:
        req = urllib.request.Request(f"{COMFYUI_URL}/history/{prompt_id}")
        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise RuntimeError(f"Не удалось обратиться к ComfyUI: {e}")

        entry = data.get(prompt_id)
        if entry is not None:
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                raise RuntimeError(f"ComfyUI сообщил об ошибке: {status}")
            return entry
        time.sleep(POLL_EVERY_SEC)

    raise TimeoutError(f"Апскейл {prompt_id} не завершился за {timeout_sec} сек")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", default=DEFAULT_INPUT_DIR,
                         help="Папка с сырыми картинками ({num}_{which}_raw.jpg)")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR,
                         help="Куда сохранять апскейленные картинки")
    parser.add_argument("--workflow", default=DEFAULT_WORKFLOW_PATH,
                         help="Путь к workflow_upscale_only.json")
    args = parser.parse_args()

    template = load_workflow_template(args.workflow)
    os.makedirs(args.output_dir, exist_ok=True)

    raw_files = sorted(f for f in os.listdir(args.input_dir) if RAW_NAME_RE.match(f))
    print(f"[i] Найдено сырых картинок: {len(raw_files)}")

    done, skipped, failed = 0, 0, 0
    for i, filename in enumerate(raw_files, 1):
        m = RAW_NAME_RE.match(filename)
        num, which = m.group(1), m.group(2)
        prefix = f"{num}_{which}"

        if already_upscaled(args.output_dir, prefix):
            skipped += 1
            continue

        print(f"[{i}/{len(raw_files)}] Апскейл {prefix}...")
        try:
            prompt_id = submit_upscale(template, filename, prefix)
            wait_for_comfy_job(prompt_id)
            print(f"  [+] Готово: {prefix}")
            done += 1
        except Exception as e:
            print(f"  [!!!] Ошибка апскейла {prefix}: {e}")
            failed += 1

    print(f"\nГотово! Апскейлено: {done}, уже было готово: {skipped}, ошибок: {failed}")


if __name__ == "__main__":
    main()
