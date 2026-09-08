# -*- coding: utf-8 -*-
"""
generate_via_api_and_upscale.py

ЧТО ДЕЛАЕТ (по-русски):
  1. Читает hook_new.csv (или другой CSV) + OBJECT_LIBRARY.md
  2. Для КАЖДОЙ сцены отправляет img_prompt_1 И img_prompt_2 в RoyalTechno
     API (модель nano-banana-2, качество "auto" — самое дешёвое, обычно
     $0.02 или бесплатно в рамках дневного лимита)
  3. Ждёт готовности картинки, скачивает её в папку ComfyUI/input
  4. Прогоняет картинку через ЛОКАЛЬНЫЙ ComfyUI — только апскейл (модель
     4x-UltraSharp), без повторной генерации — увеличивает до 2048x1152
  5. Финальный результат сохраняется в ComfyUI/output с именем вида
     001_img1.png, 001_img2.png

ТРЕБОВАНИЯ ПЕРЕД ЗАПУСКОМ:
  - ComfyUI должен быть запущен (это уже проверяется само по себе, если
    работал раньше — можно не открывать браузер, скрипт стучится по API)
  - В терминале ОБЯЗАТЕЛЬНО перед запуском выполнить:
      export ROYALTECHNO_API_KEY="твой_ключ"

КАК ЗАПУСТИТЬ:
  Сначала проверка без реальных затрат денег (просто покажет, что будет
  отправлено):
      python3 generate_via_api_and_upscale.py --limit 2

  Реальный запуск (тратит деньги/дневной лимит):
      python3 generate_via_api_and_upscale.py --run --limit 2

  Когда протестировали на 2 сценах и всё устраивает — весь файл:
      python3 generate_via_api_and_upscale.py --run
"""

import argparse
import copy
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# НАСТРОЙКИ
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CSV_PATH = os.path.join(BASE_DIR, "hook_new.csv")
LIBRARY_PATH = os.path.join(BASE_DIR, "OBJECT_LIBRARY.md")
UPSCALE_WORKFLOW_PATH = os.path.join(BASE_DIR, "workflow_upscale_only.json")

# Локальный ComfyUI (на этом же сервере)
COMFYUI_URL = "http://127.0.0.1:8188"
COMFYUI_INPUT_DIR = os.path.join(BASE_DIR, "..", "input")   # ComfyUI/input
COMFYUI_OUTPUT_DIR = os.path.join(BASE_DIR, "..", "output")  # ComfyUI/output

# RoyalTechno API
API_BASE = "https://api.royaltechno.cc/v1"
IMAGE_MODEL = "nano-banana-2"
IMAGE_QUALITY = "auto"          # самое дешёвое / бесплатное в рамках лимита
IMAGE_ASPECT_RATIO = "landscape"  # 16:9

VIDEO_MODEL = "veo-3.1"
VIDEO_DURATION_SEC = 8
VIDEO_RESOLUTION = "1080p"      # запрошенное разрешение видео (Veo 3.1)

FINAL_WIDTH = 2048
FINAL_HEIGHT = 1152

POLL_EVERY_SEC = 3
POLL_TIMEOUT_SEC = 300
MAX_RETRIES = 3          # сколько раз пробовать одну сцену при сбое API
RETRY_DELAY_SEC = 10     # пауза между попытками


# ---------------------------------------------------------------------------
# ПАРСЕР OBJECT_LIBRARY.md (та же логика, что в generate_from_csv.py)
# ---------------------------------------------------------------------------

def parse_library(path):
    if not os.path.exists(path):
        print(f"[!] Файл библиотеки не найден: {path}")
        return {}

    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    library = {}
    current_obj = None
    section = None

    TAG_RE = re.compile(r'^([A-Z][A-Z0-9_]{2,})\s*$')
    BULLET_RE = re.compile(r'^[•\-\*]\s+(.+)$')

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if not line:
            if section == "description":
                continue
            section = None
            continue

        if re.match(r'^[A-Z][A-Z &/]+$', line) and '_' not in line:
            current_obj = None
            section = None
            continue

        m = TAG_RE.match(line)
        if m:
            current_obj = {"description": "", "mandatory": [], "never": []}
            library[m.group(1)] = current_obj
            section = None
            continue

        if current_obj is None:
            continue

        if line.startswith("Description:"):
            current_obj["description"] = line[len("Description:"):].strip()
            section = "description"
            continue

        if "Mandatory features" in line:
            section = "mandatory"
            continue

        if line == "Never:":
            section = "never"
            continue

        bm = BULLET_RE.match(line)
        if bm and section in ("mandatory", "never"):
            current_obj[section].append(bm.group(1).strip())
            continue

        if section == "description":
            current_obj["description"] = (current_obj["description"] + " " + line).strip()
            continue

        if section in ("mandatory", "never") and line:
            current_obj[section].append(line)
            continue

    return library


def expand_tags(prompt_text, ref_tags_field, library):
    if not ref_tags_field or ref_tags_field.strip() in ("-", ""):
        return prompt_text

    tags = [t.strip() for t in ref_tags_field.split(",") if t.strip()]
    extras = []
    for tag in tags:
        obj = library.get(tag)
        if not obj:
            continue
        piece = obj.get("description", "")
        if obj.get("mandatory"):
            piece += " " + ", ".join(obj["mandatory"])
        if piece:
            extras.append(piece)

    if not extras:
        return prompt_text
    return prompt_text + " " + " ".join(extras)


def read_rows(csv_path):
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        return list(reader)


# ---------------------------------------------------------------------------
# ROYALTECHNO API
# ---------------------------------------------------------------------------

def _api_request(method, path, payload=None):
    key = os.environ.get("ROYALTECHNO_API_KEY")
    if not key:
        print("[!] Не найден ключ. Выполни: export ROYALTECHNO_API_KEY=\"твой_ключ\"")
        sys.exit(1)

    url = f"{API_BASE}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "curl/8.0.0",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Ошибка API {e.code}: {body}") from e


def submit_image_job(prompt):
    payload = {
        "model": IMAGE_MODEL,
        "input": {
            "prompt": prompt,
            "aspect_ratio": IMAGE_ASPECT_RATIO,
            "resolution": IMAGE_QUALITY,
        },
    }
    result = _api_request("POST", "/jobs", payload)
    return result["id"]


def submit_video_job(prompt, start_image_url):
    """
    Отправляет задачу оживления готовой картинки в видео через Veo.
    start_image_url — прямая ссылка на уже сгенерированную картинку
    (можно взять из output готовой image-задачи, или из локального файла,
    если он предварительно загружен куда-то с публичным доступом).
    """
    payload = {
        "model": VIDEO_MODEL,
        "input": {
            "prompt": prompt,
            "start_image_url": start_image_url,
            "aspect_ratio": IMAGE_ASPECT_RATIO,
            "duration_sec": VIDEO_DURATION_SEC,
            "resolution": VIDEO_RESOLUTION,
        },
    }
    result = _api_request("POST", "/jobs", payload)
    return result["id"]


def wait_for_job(job_id):
    started = time.time()
    while time.time() - started < POLL_TIMEOUT_SEC:
        result = _api_request("GET", f"/jobs/{job_id}")
        status = result.get("status")
        if status == "succeeded":
            print()  # перевод строки после точек ожидания
            return result
        if status == "failed":
            print()
            raise RuntimeError(f"Задача {job_id} завершилась с ошибкой: {result}")
        print(".", end="", flush=True)  # видимый признак, что скрипт не завис
        time.sleep(POLL_EVERY_SEC)
    print()
    raise TimeoutError(f"Задача {job_id} не завершилась за {POLL_TIMEOUT_SEC} секунд")


def download_image(url, save_path):
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0.0"})
    with urllib.request.urlopen(req) as resp:
        data = resp.read()
    with open(save_path, "wb") as f:
        f.write(data)


# ---------------------------------------------------------------------------
# ЛОКАЛЬНЫЙ COMFYUI — АПСКЕЙЛ
# ---------------------------------------------------------------------------

def load_upscale_template():
    if not os.path.exists(UPSCALE_WORKFLOW_PATH):
        print(f"[!] Не найден файл схемы апскейла: {UPSCALE_WORKFLOW_PATH}")
        sys.exit(1)
    with open(UPSCALE_WORKFLOW_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def scene_is_complete(num, which, animate_flag):
    """Проверяет, есть ли уже готовый результат для этой сцены (картинка,
    и видео, если сцена анимированная) — чтобы не тратить деньги повторно."""
    import glob
    image_pattern = os.path.join(COMFYUI_OUTPUT_DIR, f"{num}_{which}*.png")
    if not glob.glob(image_pattern):
        return False
    if animate_flag == "TRUE":
        video_path = os.path.join(COMFYUI_OUTPUT_DIR, f"{num}_{which}_video.mp4")
        if not os.path.exists(video_path):
            return False
    return True


def process_scene(num, which, full_prompt, row, upscale_template):
    """Обрабатывает одну сцену (картинка + апскейл + видео, если нужно).
    Возвращает True при успехе, False при ошибке (ошибка печатается сама)."""
    try:
        # 1. Отправляем в API
        job_id = submit_image_job(full_prompt)
        print(f"[+] Задача отправлена в API, id: {job_id}")

        # 2. Ждём готовности
        result = wait_for_job(job_id)
        image_url = result["output"]["url"]
        cost = result.get("cost_usd_cents", 0)
        print(f"[+] Готово. Стоимость: {cost} центов. URL: {image_url}")

        # 3. Скачиваем картинку в ComfyUI/input
        raw_filename = f"{num}_{which}_raw.jpg"
        raw_path = os.path.join(COMFYUI_INPUT_DIR, raw_filename)
        download_image(image_url, raw_path)
        print(f"[+] Скачано в {raw_path}")

        # 4. Апскейл через локальный ComfyUI
        upscale_result = queue_upscale(upscale_template, raw_filename, f"{num}_{which}")
        print(f"[+] Отправлено на апскейл, id задачи ComfyUI: {upscale_result.get('prompt_id')}")

        # 5. Если сцена помечена animate=TRUE — оживляем КАЖДУЮ картинку
        #    (и img1, и img2) через Veo — получаем 2 видео на сцену
        animate_flag = row.get("animate", "").strip().upper()
        video_prompt = row.get("video_prompt", "").strip()
        if animate_flag == "TRUE" and video_prompt:
            print(f"[i] Сцена {num} ({which}) помечена animate=TRUE, запускаю Veo...")
            video_job_id = submit_video_job(video_prompt, image_url)
            print(f"[+] Видео-задача отправлена, id: {video_job_id}")
            video_result = wait_for_job(video_job_id)
            video_url = video_result["output"]["url"]
            video_cost = video_result.get("cost_usd_cents", 0)
            print(f"[+] Видео готово. Стоимость: {video_cost} центов. URL: {video_url}")

            video_filename = f"{num}_{which}_video.mp4"
            video_path = os.path.join(COMFYUI_OUTPUT_DIR, video_filename)
            req = urllib.request.Request(video_url, headers={"User-Agent": "curl/8.0.0"})
            with urllib.request.urlopen(req) as resp:
                video_data = resp.read()
            with open(video_path, "wb") as f:
                f.write(video_data)
            print(f"[+] Видео сохранено: {video_path}")

        return True

    except Exception as e:
        print(f"[!] Ошибка на сцене {num} ({which}): {e}")
        return False


def queue_upscale(template, input_filename, output_prefix):
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
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# ОСНОВНОЙ ПРОЦЕСС
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true",
                         help="Реально отправлять запросы (без флага — только показ)")
    parser.add_argument("--limit", type=int, default=None,
                         help="Обработать только первые N строк (для теста)")
    parser.add_argument("--only", type=str, default=None,
                         help="Обработать только строку с указанным num")
    parser.add_argument("--start-row", type=int, default=None,
                         help="С какой строки начать (1 = первая строка данных). Для параллельной обработки в нескольких терминалах.")
    parser.add_argument("--end-row", type=int, default=None,
                         help="На какой строке закончить (включительно). Используй вместе с --start-row.")
    parser.add_argument("--csv", type=str, default=None,
                         help="Имя CSV-файла для обработки (например 1_block.csv). Если не указано — используется CSV_PATH из настроек скрипта.")
    args = parser.parse_args()

    csv_path = os.path.join(BASE_DIR, args.csv) if args.csv else CSV_PATH

    os.makedirs(COMFYUI_INPUT_DIR, exist_ok=True)

    library = parse_library(LIBRARY_PATH)
    print(f"[i] Библиотека объектов: {len(library)} тегов найдено")

    rows = read_rows(csv_path)
    print(f"[i] Файл: {csv_path}")
    print(f"[i] Сценарий: {len(rows)} строк найдено")

    if args.only:
        rows = [r for r in rows if r.get("num", "").strip() == args.only]
    elif args.start_row is not None or args.end_row is not None:
        start = (args.start_row or 1) - 1  # переводим в индекс с 0
        end = args.end_row if args.end_row is not None else len(rows)
        rows = rows[start:end]
        print(f"[i] Обрабатываю строки с {args.start_row or 1} по {args.end_row or len(rows)}")
    elif args.limit:
        rows = rows[:args.limit]

    upscale_template = load_upscale_template() if args.run else None

    failed_scenes = []

    for row in rows:
        num = row.get("num", "").strip()
        if not num or not row.get("img_prompt_1", "").strip():
            continue

        ref_tags = row.get("ref_tags", "").strip()
        animate_flag = row.get("animate", "").strip().upper()

        for which, col in (("img1", "img_prompt_1"), ("img2", "img_prompt_2")):
            base_prompt = row.get(col, "").strip()
            if not base_prompt or base_prompt == "-":
                print(f"[-] Строка {num}: нет текста в {col}, пропускаю")
                continue

            full_prompt = expand_tags(base_prompt, ref_tags, library)
            print(f"\n=== Сцена {num} ({which}) ===")
            print(f"Теги: {ref_tags or '(нет)'}")
            print(f"Промпт (первые 200 символов): {full_prompt[:200]}...")

            if not args.run:
                print("[i] Сухой прогон — ничего не отправлено. Добавь --run для реальной генерации.")
                continue

            if scene_is_complete(num, which, animate_flag):
                print(f"[=] Сцена {num} ({which}) уже готова, пропускаю (экономим деньги).")
                continue

            success = False
            for attempt in range(1, MAX_RETRIES + 1):
                if attempt > 1:
                    print(f"[i] Попытка {attempt}/{MAX_RETRIES} для сцены {num} ({which})...")
                success = process_scene(num, which, full_prompt, row, upscale_template)
                if success:
                    break
                if attempt < MAX_RETRIES:
                    print(f"[!] Жду {RETRY_DELAY_SEC} сек перед следующей попыткой...")
                    time.sleep(RETRY_DELAY_SEC)

            if not success:
                print(f"[!!!] Сцена {num} ({which}) не удалась после {MAX_RETRIES} попыток.")
                failed_scenes.append(f"{num} ({which})")

            time.sleep(1)

    print("\nГотово. Финальные картинки появятся в ComfyUI/output через несколько секунд после апскейла.")

    if args.run and failed_scenes:
        print(f"\n[!!!] Не удалось сгенерировать {len(failed_scenes)} сцен(ы) после {MAX_RETRIES} попыток каждая:")
        for s in failed_scenes:
            print(f"    - {s}")
        print("Запусти скрипт с тем же --csv ещё раз (без --limit/--only) — "
              "уже готовые сцены будут пропущены автоматически, обработаются только оставшиеся.")


if __name__ == "__main__":
    main()
