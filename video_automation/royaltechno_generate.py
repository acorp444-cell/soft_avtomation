"""
Генерация картинок и видео через RoyalTechno API БЕЗ УЧАСТИЯ RUNPOD.

Раньше весь процесс (картинка -> апскейл -> видео) выполнялся одним
скриптом на сервере RunPod (generate_via_api_and_upscale.py), и сервер
с видеокартой был включён всё время, пока скрипт просто ждал ответа от
RoyalTechno - хотя для этого ожидания видеокарта не нужна вообще.

Здесь - только те шаги, для которых видеокарта НЕ нужна, поэтому они
выполняются прямо на компьютере пользователя:

  1. generate_images() - отправляет img_prompt_1/img_prompt_2 в
     RoyalTechno, ждёт, скачивает СЫРЫЕ (ещё не апскейленные) картинки
     локально.
  2. (между этим шагом и следующим - апскейл на RunPod, отдельным
     скриптом upscale_batch.py, см. кнопку "Апскейл на RunPod")
  3. generate_videos_from_upscaled() - берёт уже апскейленную картинку,
     кодирует в base64 (RoyalTechno принимает инлайн-картинку без
     отдельной загрузки/хостинга - см. документацию, раздел "Image
     inputs", inline data URI), отправляет в Veo, ждёт, скачивает видео.

Обе функции безопасны для повторного запуска - уже готовые файлы не
перегенерируются заново (не тратятся деньги повторно).
"""

import base64
import csv
import io
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

API_BASE = "https://api.royaltechno.cc/v1"
IMAGE_MODEL = "nano-banana-2"
IMAGE_QUALITY = "auto"
IMAGE_ASPECT_RATIO = "landscape"

VIDEO_MODEL = "veo-3.1"
VIDEO_DURATION_SEC = 8
VIDEO_RESOLUTION = "1080p"

POLL_EVERY_SEC = 3
POLL_TIMEOUT_SEC = 300
MAX_RETRIES = 3
RETRY_DELAY_SEC = 10

# Лимит RoyalTechno на инлайн-картинку (data URI) - 5 МиБ после
# раскодирования. Апскейленный PNG (2048x1152) легко может быть тяжелее,
# поэтому перед отправкой в видео пересжимаем в JPEG хорошего качества -
# так гарантированно укладываемся в лимит.
INLINE_IMAGE_JPEG_QUALITY = 90
INLINE_IMAGE_MAX_BYTES = 5 * 1024 * 1024


# ---------------------------------------------------------------------------
# БИБЛИОТЕКА ОБЪЕКТОВ (та же логика, что в generate_via_api_and_upscale.py)
# ---------------------------------------------------------------------------

def parse_library(path):
    if not os.path.exists(path):
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

def _api_request(method, path, api_key, payload=None):
    url = f"{API_BASE}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "curl/8.0.0",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Ошибка RoyalTechno API {e.code}: {body}") from e


def submit_image_job(prompt, api_key):
    payload = {
        "model": IMAGE_MODEL,
        "input": {
            "prompt": prompt,
            "aspect_ratio": IMAGE_ASPECT_RATIO,
            "resolution": IMAGE_QUALITY,
        },
    }
    result = _api_request("POST", "/jobs", api_key, payload)
    return result["id"]


def submit_video_job(prompt, start_image_source, api_key):
    """start_image_source - либо обычная ссылка (str, начинается с http),
    либо инлайн data URI (str, уже начинается с 'data:')."""
    payload = {
        "model": VIDEO_MODEL,
        "input": {
            "prompt": prompt,
            "start_image_url": start_image_source,
            "duration_sec": VIDEO_DURATION_SEC,
            "resolution": VIDEO_RESOLUTION,
        },
    }
    result = _api_request("POST", "/jobs", api_key, payload)
    return result["id"]


def wait_for_job(job_id, api_key, on_progress=None):
    started = time.time()
    while time.time() - started < POLL_TIMEOUT_SEC:
        result = _api_request("GET", f"/jobs/{job_id}", api_key)
        status = result.get("status")
        if status == "succeeded":
            return result
        if status == "failed":
            raise RuntimeError(f"Задача {job_id} завершилась с ошибкой: {result}")
        if on_progress:
            on_progress()
        time.sleep(POLL_EVERY_SEC)
    raise TimeoutError(f"Задача {job_id} не завершилась за {POLL_TIMEOUT_SEC} секунд")


def _submit_and_wait_with_retries(submit_fn, api_key, log, label):
    """Обёртка с повторными попытками вокруг отправки+ожидания одной
    задачи - сетевые сбои/временные ошибки API не должны сразу обрывать
    всю пачку."""
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            job_id = submit_fn()
            result = wait_for_job(job_id, api_key)
            return result
        except Exception as e:
            last_error = e
            log(f"  [!] {label}: попытка {attempt}/{MAX_RETRIES} не удалась ({e})")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SEC)
    raise RuntimeError(f"{label}: не удалось после {MAX_RETRIES} попыток ({last_error})")


def download_url(url, save_path):
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0.0"})
    with urllib.request.urlopen(req) as resp:
        data = resp.read()
    Path(save_path).parent.mkdir(parents=True, exist_ok=True)
    with open(save_path, "wb") as f:
        f.write(data)


# ---------------------------------------------------------------------------
# ШАГ 1: ГЕНЕРАЦИЯ СЫРЫХ КАРТИНОК (без RunPod)
# ---------------------------------------------------------------------------

def generate_images(csv_path, library_path, output_dir, api_key, log=print,
                     limit=None, should_stop=None):
    """Для каждой сцены в CSV отправляет img_prompt_1 и img_prompt_2 в
    RoyalTechno, скачивает сырые (не апскейленные) картинки в output_dir
    с именами {num}_{which}_raw.jpg - такое же имя, которое ожидает
    апскейл на RunPod. Уже готовые файлы не перегенерируются.

    should_stop - необязательная функция без аргументов, возвращающая
    True, если нужно прервать процесс (для кнопки отмены)."""
    library = parse_library(library_path)
    log(f"[i] Библиотека объектов: {len(library)} тегов найдено")

    rows = read_rows(csv_path)
    if limit:
        rows = rows[:limit]
    log(f"[i] Сцен в CSV: {len(rows)}")

    os.makedirs(output_dir, exist_ok=True)

    done, skipped, failed = 0, 0, 0
    for row in rows:
        if should_stop and should_stop():
            log("[!] Остановлено пользователем.")
            break

        num = row.get("num", "").strip()
        if not num:
            continue
        ref_tags = row.get("ref_tags", "").strip()

        for which, col in (("img1", "img_prompt_1"), ("img2", "img_prompt_2")):
            base_prompt = row.get(col, "").strip()
            if not base_prompt or base_prompt == "-":
                continue

            raw_path = os.path.join(output_dir, f"{num}_{which}_raw.jpg")
            if os.path.exists(raw_path):
                skipped += 1
                continue

            full_prompt = expand_tags(base_prompt, ref_tags, library)
            log(f"=== Сцена {num} ({which}) - запрос картинки в RoyalTechno...")

            try:
                result = _submit_and_wait_with_retries(
                    lambda: submit_image_job(full_prompt, api_key),
                    api_key, log, f"картинка {num}/{which}",
                )
                image_url = result["output"]["url"]
                cost = result.get("cost_usd_cents", 0)
                download_url(image_url, raw_path)
                log(f"  [+] Готово, стоимость {cost} центов, сохранено: {raw_path}")
                done += 1
            except Exception as e:
                log(f"  [!!!] Сцена {num} ({which}): не удалось сгенерировать картинку: {e}")
                failed += 1

    log(f"\nГотово! Сгенерировано: {done}, уже было готово: {skipped}, ошибок: {failed}")


# ---------------------------------------------------------------------------
# ШАГ 3: ВИДЕО ИЗ УЖЕ АПСКЕЙЛЕННОЙ КАРТИНКИ (без RunPod)
# ---------------------------------------------------------------------------

def _find_upscaled_image(upscaled_dir, num, which):
    """ComfyUI дописывает свой счётчик к имени файла (например
    075_img1_00001_.png) - ищем по маске, берём последний (самый свежий)."""
    import glob
    pattern = os.path.join(upscaled_dir, f"{num}_{which}_*.png")
    matches = sorted(glob.glob(pattern))
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
            "python -m pip install Pillow"
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


def generate_videos_from_upscaled(csv_path, upscaled_dir, output_dir, api_key,
                                   log=print, should_stop=None):
    """Для каждой сцены с animate=TRUE берёт уже апскейленную картинку
    (из upscaled_dir, скачанную с RunPod после апскейла), отправляет её
    в RoyalTechno/Veo как инлайн-картинку (без отдельной загрузки куда-
    либо) и скачивает готовое видео в output_dir с именем
    {num}_{which}_video.mp4 - таким же, какое ожидает финальная сборка
    видео (кнопка 12)."""
    rows = read_rows(csv_path)
    os.makedirs(output_dir, exist_ok=True)

    done, skipped, failed = 0, 0, 0
    for row in rows:
        if should_stop and should_stop():
            log("[!] Остановлено пользователем.")
            break

        num = row.get("num", "").strip()
        animate_flag = row.get("animate", "").strip().upper()
        video_prompt = row.get("video_prompt", "").strip()
        if not num or animate_flag != "TRUE" or not video_prompt:
            continue

        for which in ("img1", "img2"):
            video_path = os.path.join(output_dir, f"{num}_{which}_video.mp4")
            if os.path.exists(video_path):
                skipped += 1
                continue

            upscaled_path = _find_upscaled_image(upscaled_dir, num, which)
            if not upscaled_path:
                log(f"  [!] Сцена {num} ({which}): нет апскейленной картинки в {upscaled_dir}, "
                    f"пропускаю (сначала нужен апскейл на RunPod)")
                failed += 1
                continue

            log(f"=== Сцена {num} ({which}) - оживляю {os.path.basename(upscaled_path)}...")
            try:
                data_uri = _image_to_data_uri(upscaled_path)
                result = _submit_and_wait_with_retries(
                    lambda: submit_video_job(video_prompt, data_uri, api_key),
                    api_key, log, f"видео {num}/{which}",
                )
                video_url = result["output"]["url"]
                cost = result.get("cost_usd_cents", 0)
                download_url(video_url, video_path)
                log(f"  [+] Готово, стоимость {cost} центов, сохранено: {video_path}")
                done += 1
            except Exception as e:
                log(f"  [!!!] Сцена {num} ({which}): не удалось сгенерировать видео: {e}")
                failed += 1

    log(f"\nГотово! Сгенерировано видео: {done}, уже было готово: {skipped}, ошибок: {failed}")
