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
     4x-UltraSharp), без повторной генерации — увеличивает до 2048x1152,
     и ЖДЁТ завершения апскейла
  5. Финальный результат сохраняется в ComfyUI/output с именем вида
     001_img1.png, 001_img2.png
  6. Если сцена помечена animate=TRUE — оживляет через Veo УЖЕ
     АПСКЕЙЛЕННУЮ картинку (не сырую), чтобы видео было того же
     качества, что и финальные кадры

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
import base64
import copy
import csv
import difflib
import glob
import io
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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

# Обычный браузерный User-Agent - "curl/8.0.0" некоторые CDN/защита от
# ботов режут/обрывают, хотя тот же запрос из настоящего браузера проходит.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT_SEC = 60
DOWNLOAD_TIMEOUT_SEC = 180

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
POLL_TIMEOUT_SEC = 1200       # при задержках на стороне RoyalTechno картинки тоже могут идти намного дольше 5 минут
VIDEO_POLL_TIMEOUT_SEC = 1200  # видео (Veo) при задержках на стороне RoyalTechno может идти намного дольше 5 минут
MAX_RETRIES = 3          # сколько раз пробовать одну сцену при сбое API
RETRY_DELAY_SEC = 10     # пауза между попытками

# Ожидание завершения апскейла в ЛОКАЛЬНОМ ComfyUI (отдельные тайминги от
# ожидания задач RoyalTechno выше - апскейл обычно намного быстрее)
COMFY_POLL_EVERY_SEC = 2
COMFY_POLL_TIMEOUT_SEC = 180

# Лимит RoyalTechno на инлайн-картинку (data URI) - 5 МиБ после
# раскодирования. Апскейленный PNG (2048x1152) легко может быть тяжелее,
# поэтому перед отправкой в видео пересжимаем в JPEG хорошего качества -
# так гарантированно укладываемся в лимит.
INLINE_IMAGE_JPEG_QUALITY = 90
INLINE_IMAGE_MAX_BYTES = 5 * 1024 * 1024


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

        if re.match(r'^[A-Z][A-Z &/]+$', line) and ' ' in line:
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


def _find_closest_tag(tag, library):
    """Ищет похожий тег в библиотеке, если точного совпадения нет -
    например, если модель написала тег с опечаткой или другим написанием
    (CHERNOBYL вместо CHORNOBYL, лишняя/переставленная буква). Порог 0.72
    подобран по реальным случаям - ловит такие опечатки, но не путает
    вообще разные (несуществующие) теги с существующими."""
    matches = difflib.get_close_matches(tag, library.keys(), n=1, cutoff=0.72)
    return matches[0] if matches else None


def expand_tags(prompt_text, ref_tags_field, library):
    if not ref_tags_field or ref_tags_field.strip() in ("-", ""):
        return prompt_text

    tags = [t.strip() for t in ref_tags_field.split(",") if t.strip()]
    extras = []
    for tag in tags:
        obj = library.get(tag)
        if not obj:
            closest = _find_closest_tag(tag, library)
            if closest:
                obj = library.get(closest)
                print(f"  [i] Тег '{tag}' не найден дословно в OBJECT_LIBRARY.md - "
                      f"использую похожий тег '{closest}' (возможно, опечатка)")
        if not obj:
            print(f"  [i] Тег '{tag}' не найден в OBJECT_LIBRARY.md - пропускаю "
                  f"(если он должен там быть, но появляется только 1 раз в сценарии, "
                  f"так и задумано; если появляется несколько раз - библиотеку стоит "
                  f"перегенерировать/дополнить)")
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
            "User-Agent": BROWSER_USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Ошибка API {e.code}: {body}") from e
    except TimeoutError as e:
        raise RuntimeError(
            f"RoyalTechno не ответил за {REQUEST_TIMEOUT_SEC} сек ({method} {path}) - "
            f"сервер завис или проблема с сетью") from e


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


def submit_video_job(prompt, start_image_source):
    """
    Отправляет задачу оживления готовой картинки в видео через Veo.
    start_image_source — либо обычная ссылка (str, начинается с http),
    либо инлайн data URI (str, начинается с 'data:' - см. _image_to_data_uri).
    """
    payload = {
        "model": VIDEO_MODEL,
        "input": {
            "prompt": prompt,
            "start_image_url": start_image_source,
            "aspect_ratio": IMAGE_ASPECT_RATIO,
            "duration_sec": VIDEO_DURATION_SEC,
            "resolution": VIDEO_RESOLUTION,
        },
    }
    result = _api_request("POST", "/jobs", payload)
    return result["id"]


def wait_for_job(job_id, timeout_sec=POLL_TIMEOUT_SEC):
    started = time.time()
    while time.time() - started < timeout_sec:
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
    raise TimeoutError(f"Задача {job_id} не завершилась за {timeout_sec} секунд")


def download_image(url, save_path):
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_SEC) as resp:
            data = resp.read()
    except TimeoutError as e:
        raise RuntimeError(f"Скачивание не ответило за {DOWNLOAD_TIMEOUT_SEC} сек: {url}") from e
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
    image_pattern = os.path.join(COMFYUI_OUTPUT_DIR, f"{num}_{which}*.png")
    if not glob.glob(image_pattern):
        return False
    if animate_flag == "TRUE":
        video_path = os.path.join(COMFYUI_OUTPUT_DIR, f"{num}_{which}_video.mp4")
        if not os.path.exists(video_path):
            return False
    return True


def wait_for_comfy_job(prompt_id, timeout_sec=COMFY_POLL_TIMEOUT_SEC):
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
                raise RuntimeError(f"ComfyUI сообщил об ошибке апскейла: {status}")
            return entry
        time.sleep(COMFY_POLL_EVERY_SEC)

    raise TimeoutError(f"Апскейл {prompt_id} не завершился за {timeout_sec} сек")


def find_upscaled_file(prefix):
    matches = sorted(glob.glob(os.path.join(COMFYUI_OUTPUT_DIR, f"{prefix}_*.png")))
    return matches[-1] if matches else None


def _image_to_data_uri(image_path):
    """Пересжимает картинку в JPEG нужного качества и кодирует в base64
    data URI - чтобы гарантированно уложиться в лимит RoyalTechno (5 МиБ
    после раскодирования) независимо от того, насколько тяжёлый PNG
    получился после апскейла."""
    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError(
            "Не установлена библиотека Pillow (нужна, чтобы пересжать картинку "
            "в JPEG перед отправкой в видео-генерацию). Выполни: "
            "python3 -m pip install Pillow"
        )

    img = Image.open(image_path).convert("RGB")
    buf = io.BytesIO()
    quality = INLINE_IMAGE_JPEG_QUALITY
    while True:
        buf.seek(0)
        buf.truncate()
        img.save(buf, format="JPEG", quality=quality)
        if buf.tell() <= INLINE_IMAGE_MAX_BYTES or quality <= 40:
            break
        quality -= 10  # картинка всё ещё слишком тяжёлая - снижаем качество и пробуем снова

    if buf.tell() > INLINE_IMAGE_MAX_BYTES:
        raise RuntimeError(
            f"Картинка {image_path} весит {buf.tell()} байт даже после сжатия "
            f"до качества {quality} - больше лимита RoyalTechno (5 МиБ)"
        )

    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def process_scene_image(num, which, full_prompt, upscale_template):
    """Картинка + апскейл для одной сцены (без видео - видео теперь
    отдельная, независимая очередь, см. process_scene_video). Возвращает
    путь к готовому апскейленному файлу. Бросает исключение при ошибке
    (её печатает вызывающий код)."""
    # 1. Отправляем в API
    job_id = submit_image_job(full_prompt)
    print(f"[+] Сцена {num} ({which}): задача отправлена в API, id: {job_id}")

    # 2. Ждём готовности
    result = wait_for_job(job_id)
    image_url = result["output"]["url"]
    cost = result.get("cost_usd_cents", 0)
    print(f"[+] Сцена {num} ({which}): картинка готова. Стоимость: {cost} центов.")

    # 3. Скачиваем картинку в ComfyUI/input
    raw_filename = f"{num}_{which}_raw.jpg"
    raw_path = os.path.join(COMFYUI_INPUT_DIR, raw_filename)
    download_image(image_url, raw_path)

    # 4. Апскейл через локальный ComfyUI - ЖДЁМ завершения (нужен готовый
    #    файл, чтобы можно было сразу поставить видео в очередь)
    prefix = f"{num}_{which}"
    upscale_result = queue_upscale(upscale_template, raw_filename, prefix)
    upscale_prompt_id = upscale_result.get("prompt_id")
    print(f"[+] Сцена {num} ({which}): отправлено на апскейл, жду завершения...")
    wait_for_comfy_job(upscale_prompt_id)
    upscaled_path = find_upscaled_file(prefix)
    if not upscaled_path:
        raise RuntimeError(f"Апскейл {prefix} завершился, но файл результата "
                            f"не найден в {COMFYUI_OUTPUT_DIR}")
    print(f"[+] Сцена {num} ({which}): апскейл готов: {upscaled_path}")
    return upscaled_path


def process_scene_video(num, which, video_prompt, upscaled_path):
    """Видео из уже готовой (апскейленной) картинки - отдельная задача от
    process_scene_image, ставится в СВОЮ, независимую очередь сразу же,
    как только картинка этой сцены готова, не дожидаясь остальных сцен."""
    start_image_source = _image_to_data_uri(upscaled_path)
    video_job_id = submit_video_job(video_prompt, start_image_source)
    print(f"[+] Сцена {num} ({which}): видео-задача отправлена, id: {video_job_id}")
    video_result = wait_for_job(video_job_id, timeout_sec=VIDEO_POLL_TIMEOUT_SEC)
    video_url = video_result["output"]["url"]
    video_cost = video_result.get("cost_usd_cents", 0)
    print(f"[+] Сцена {num} ({which}): видео готово. Стоимость: {video_cost} центов.")

    video_filename = f"{num}_{which}_video.mp4"
    video_path = os.path.join(COMFYUI_OUTPUT_DIR, video_filename)
    req = urllib.request.Request(video_url, headers={"User-Agent": BROWSER_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_SEC) as resp:
            video_data = resp.read()
    except TimeoutError as e:
        raise RuntimeError(
            f"Скачивание видео не ответило за {DOWNLOAD_TIMEOUT_SEC} сек: {video_url}") from e
    with open(video_path, "wb") as f:
        f.write(video_data)
    print(f"[+] Сцена {num} ({which}): видео сохранено: {video_path}")


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
    parser.add_argument("--video-resolution", choices=["1080p", "720p"], default=None,
                         help="Переопределить разрешение видео (Veo) - например 720p, если "
                              "RoyalTechno сообщает о задержках с апскейлом до 1080p")
    parser.add_argument("--max-parallel-images", type=int, default=3,
                         help="Сколько картинок (+апскейл) обрабатывать одновременно (по "
                              "умолчанию 3, как позволяет обычный тариф RoyalTechno)")
    parser.add_argument("--max-parallel-video", type=int, default=3,
                         help="Сколько видео генерировать одновременно (по умолчанию 3) - "
                              "своя, независимая очередь: видео сцены запускается сразу, как "
                              "только готова её картинка, не дожидаясь остальных сцен")
    args = parser.parse_args()

    csv_path = os.path.join(BASE_DIR, args.csv) if args.csv else CSV_PATH

    if args.video_resolution:
        global VIDEO_RESOLUTION
        VIDEO_RESOLUTION = args.video_resolution
        print(f"[i] Разрешение видео переопределено: {VIDEO_RESOLUTION}")

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

    # сначала собираем список сцен, которые реально нужно обрабатывать
    # (печатаем промты сразу тут же, по порядку - для сухого прогона это
    # единственное, что вообще происходит) - а саму генерацию запускаем
    # параллельно ниже, а не по одной сцене за раз
    tasks = []
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

            tasks.append((num, which, full_prompt, row))

    if not args.run:
        return

    if not tasks:
        print("\nВсе сцены уже готовы, обрабатывать нечего.")
        return

    failed_scenes = []
    failed_lock = threading.Lock()
    video_futures = []
    video_futures_lock = threading.Lock()

    video_executor = ThreadPoolExecutor(max_workers=args.max_parallel_video)

    def run_video_with_retries(num, which, video_prompt, upscaled_path):
        success = False
        for attempt in range(1, MAX_RETRIES + 1):
            if attempt > 1:
                print(f"[i] Сцена {num} ({which}): попытка видео {attempt}/{MAX_RETRIES}...")
            try:
                process_scene_video(num, which, video_prompt, upscaled_path)
                success = True
                break
            except Exception as e:
                print(f"[!] Сцена {num} ({which}): ошибка видео: {e}")
                if attempt < MAX_RETRIES:
                    print(f"[!] Жду {RETRY_DELAY_SEC} сек перед следующей попыткой...")
                    time.sleep(RETRY_DELAY_SEC)
        if not success:
            print(f"[!!!] Сцена {num} ({which}): видео не удалось после {MAX_RETRIES} попыток.")
            with failed_lock:
                failed_scenes.append(f"{num} ({which}) - видео")

    def run_image_with_retries(task):
        num, which, full_prompt, row = task
        upscaled_path = None
        for attempt in range(1, MAX_RETRIES + 1):
            if attempt > 1:
                print(f"[i] Сцена {num} ({which}): попытка картинки {attempt}/{MAX_RETRIES}...")
            try:
                upscaled_path = process_scene_image(num, which, full_prompt, upscale_template)
                break
            except Exception as e:
                print(f"[!] Сцена {num} ({which}): ошибка картинки: {e}")
                if attempt < MAX_RETRIES:
                    print(f"[!] Жду {RETRY_DELAY_SEC} сек перед следующей попыткой...")
                    time.sleep(RETRY_DELAY_SEC)

        if upscaled_path is None:
            print(f"[!!!] Сцена {num} ({which}): картинка не удалась после {MAX_RETRIES} попыток.")
            with failed_lock:
                failed_scenes.append(f"{num} ({which}) - картинка")
            return

        # видео этой сцены ставим в очередь СРАЗУ, не дожидаясь остальных
        # картинок - картинки и видео теперь две независимые очереди
        animate_flag = task[3].get("animate", "").strip().upper()
        video_prompt = task[3].get("video_prompt", "").strip()
        if animate_flag == "TRUE" and video_prompt:
            future = video_executor.submit(run_video_with_retries, num, which, video_prompt, upscaled_path)
            with video_futures_lock:
                video_futures.append(future)

    print(f"\n[i] Запускаю картинки (+ апскейл) с параллелизмом {args.max_parallel_images}, "
          f"видео - с параллелизмом {args.max_parallel_video} (своя очередь: видео сцены "
          f"стартует сразу, как только готова её картинка, не дожидаясь остальных сцен)...")
    with ThreadPoolExecutor(max_workers=args.max_parallel_images) as image_executor:
        image_futures = [image_executor.submit(run_image_with_retries, t) for t in tasks]
        for f in image_futures:
            f.result()

    # все картинки обработаны, а значит и все видео-задачи, которые из них
    # появляются, уже поставлены в очередь video_executor - остаётся
    # дождаться, пока эта очередь тоже опустеет
    for f in video_futures:
        f.result()
    video_executor.shutdown(wait=True)

    print("\nГотово. Финальные картинки и видео появятся в ComfyUI/output.")

    if args.run and failed_scenes:
        print(f"\n[!!!] Не удалось сгенерировать {len(failed_scenes)} сцен(ы) после {MAX_RETRIES} попыток каждая:")
        for s in failed_scenes:
            print(f"    - {s}")
        print("Запусти скрипт с тем же --csv ещё раз (без --limit/--only) — "
              "уже готовые сцены будут пропущены автоматически, обработаются только оставшиеся.")


if __name__ == "__main__":
    main()
