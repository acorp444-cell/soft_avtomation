"""
Генерация картинок и видео через Google Gemini API (Nano Banana + Veo)
НАПРЯМУЮ, без RunPod и без RoyalTechno - запасной вариант на случай, если
RoyalTechno недоступен или работает медленно.

Публичный интерфейс (generate_images, generate_videos_from_upscaled)
намеренно совпадает по сигнатуре с royaltechno_generate.py - это
позволяет app.py выбирать провайдера одной настройкой, не меняя код
вызова. Общие вспомогательные функции (парсинг библиотеки объектов,
CSV, интерактивная остановка, параллельный запуск задач) переиспользуются
из royaltechno_generate.py, а не дублируются.

Картинки (Nano Banana / Gemini 2.5 Flash Image) генерируются синхронно -
готовая картинка приходит в том же самом ответе, без отдельного опроса
статуса задачи, как у RoyalTechno.

Видео (Veo) наоборот работает через "долгую операцию" (long-running
operation) - сначала отправляется запрос на генерацию, в ответ приходит
имя операции, дальше нужно самим периодически спрашивать её статус, пока
не появится готовое видео.
"""

import base64
import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from royaltechno_generate import (
    GenerationStopped,
    _sleep_interruptible,
    _make_progress_logger,
    _run_tasks_parallel,
    _looks_like_valid_image,
    _looks_like_valid_video,
    _find_upscaled_image,
    _image_to_data_uri,
    parse_library,
    expand_tags,
    read_rows,
    format_elapsed,
)

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
IMAGE_MODEL = "gemini-2.5-flash-image-preview"
IMAGE_ASPECT_RATIO = "16:9"  # как у RoyalTechno (landscape) - кадры для видео, не квадратные
IMAGE_SIZE = "2K"  # 1K/2K/4K - с запасом для последующего апскейла на RunPod
VIDEO_MODEL = "veo-3.1-generate-preview"

VIDEO_DURATION_SEC = 8
VIDEO_RESOLUTION = "1080p"  # "1080p" или "720p"
VIDEO_ASPECT_RATIO = "16:9"

POLL_EVERY_SEC = 5
POLL_TIMEOUT_SEC = 300
VIDEO_POLL_TIMEOUT_SEC = 1200
MAX_RETRIES = 3
RETRY_DELAY_SEC = 10

REQUEST_TIMEOUT_SEC = 60
DOWNLOAD_TIMEOUT_SEC = 180

DEFAULT_MAX_PARALLEL = 2  # у Google свои лимиты одновременных запросов, обычно строже, чем у RoyalTechno


def _run_curl(cmd, timeout_sec, what):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec + 10)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"{what} не ответил за {timeout_sec} сек") from e
    except FileNotFoundError as e:
        raise RuntimeError(
            "Не найдена программа 'curl' на этом компьютере (обычно она уже "
            "встроена в Windows 10/11 - проверь командой 'curl -version' в "
            "командной строке)") from e


def _api_request(method, path, api_key, payload=None, timeout_sec=REQUEST_TIMEOUT_SEC):
    url = f"{API_BASE}{path}"
    cmd = ["curl", "-s", "-S", "-X", method, url,
           "--max-time", str(timeout_sec),
           "-H", f"x-goog-api-key: {api_key}",
           "-H", "Content-Type: application/json",
           "-w", "\n%{http_code}"]

    tmp_path = None
    if payload is not None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            json.dump(payload, tmp)
            tmp_path = tmp.name
        cmd += ["-d", f"@{tmp_path}"]

    try:
        result = _run_curl(cmd, timeout_sec, f"Google API ({method} {path})")
    finally:
        if tmp_path:
            os.unlink(tmp_path)

    if result.returncode != 0:
        raise RuntimeError(f"curl не смог связаться с Google API (код {result.returncode}): "
                            f"{result.stderr.strip()[-500:]}")

    body, _, status_code = result.stdout.rpartition("\n")
    try:
        status = int(status_code)
    except ValueError:
        raise RuntimeError(f"Не удалось разобрать ответ Google API: {result.stdout[:500]}")

    if status >= 400:
        raise RuntimeError(f"Ошибка Google API {status}: {body[:800]}")

    return json.loads(body)


# ---------------------------------------------------------------------------
# КАРТИНКИ (Nano Banana / Gemini 2.5 Flash Image) - синхронно, без опроса
# ---------------------------------------------------------------------------

def _request_image(prompt, api_key):
    """Один запрос на картинку. В отличие от RoyalTechno, тут нет отдельного
    job_id и опроса статуса - готовая картинка (в base64) приходит сразу в
    ответе, если он вообще пришёл успешно."""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "imageConfig": {
                "aspectRatio": IMAGE_ASPECT_RATIO,
                "imageSize": IMAGE_SIZE,
            },
        },
    }
    result = _api_request("POST", f"/models/{IMAGE_MODEL}:generateContent", api_key, payload)
    candidates = result.get("candidates") or []
    parts = (candidates[0].get("content", {}).get("parts", []) if candidates else [])
    for part in parts:
        inline = part.get("inlineData")
        if inline and inline.get("data"):
            return inline["data"]
    raise RuntimeError(f"В ответе Google не найдена картинка: {json.dumps(result)[:500]}")


def _request_image_with_retries(prompt, api_key, log, label, should_stop=None):
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        if should_stop and should_stop():
            raise GenerationStopped()
        try:
            return _request_image(prompt, api_key)
        except Exception as e:
            last_error = e
            log(f"  [!] {label}: попытка {attempt}/{MAX_RETRIES} не удалась ({e})")
            if attempt < MAX_RETRIES:
                _sleep_interruptible(RETRY_DELAY_SEC, should_stop)
    raise RuntimeError(f"{label}: не удалось после {MAX_RETRIES} попыток ({last_error})")


def generate_images(csv_path, library_path, output_dir, api_key, log=print,
                     limit=None, should_stop=None, max_parallel=DEFAULT_MAX_PARALLEL):
    """Сигнатура совпадает с royaltechno_generate.generate_images - см. её
    докстринг. Отличие только в том, какой провайдер реально рисует
    картинку (Google Nano Banana вместо RoyalTechno)."""
    library = parse_library(library_path)
    log(f"[i] Библиотека объектов: {len(library)} тегов найдено")

    rows = read_rows(csv_path)
    if limit:
        rows = rows[:limit]
    log(f"[i] Сцен в CSV: {len(rows)}")

    os.makedirs(output_dir, exist_ok=True)

    counters = {"done": 0, "skipped": 0, "failed": 0}
    counters_lock = threading.Lock()

    infographic_skipped = 0
    tasks = []
    for row in rows:
        num = row.get("num", "").strip()
        if not num:
            continue
        if (row.get("source") or "").strip() == "AI_INFOGRAPHIC":
            infographic_skipped += 1
            continue
        ref_tags = row.get("ref_tags", "").strip()
        for which, col in (("img1", "img_prompt_1"), ("img2", "img_prompt_2")):
            base_prompt = row.get(col, "").strip()
            if not base_prompt or base_prompt == "-":
                continue
            raw_path = os.path.join(output_dir, f"{num}_{which}_raw.jpg")
            tasks.append((num, which, base_prompt, ref_tags, raw_path))

    if infographic_skipped:
        log(f"[i] Кадров с инфографикой (source=AI_INFOGRAPHIC): {infographic_skipped} - "
            f"пропускаю здесь, они генерируются через OpenAI на шаге B")

    def process_one(task):
        num, which, base_prompt, ref_tags, raw_path = task
        if should_stop and should_stop():
            return
        if os.path.exists(raw_path):
            if _looks_like_valid_image(raw_path):
                with counters_lock:
                    counters["skipped"] += 1
                return
            log(f"  [!] Сцена {num} ({which}): найденный файл повреждён - перегенерирую: {raw_path}")
            os.remove(raw_path)

        full_prompt = expand_tags(base_prompt, ref_tags, library, log=log)
        task_started = time.time()
        log(f"=== Сцена {num} ({which}) - запрос картинки в Google (Nano Banana)...")
        try:
            b64_data = _request_image_with_retries(full_prompt, api_key, log, f"картинка {num}/{which}",
                                                     should_stop=should_stop)
            Path(raw_path).parent.mkdir(parents=True, exist_ok=True)
            with open(raw_path, "wb") as f:
                f.write(base64.b64decode(b64_data))
            if not _looks_like_valid_image(raw_path):
                raise RuntimeError("полученная картинка повреждена")
            log(f"  [+] Сцена {num} ({which}) готово за {format_elapsed(time.time() - task_started)}, "
                f"сохранено: {raw_path}")
            with counters_lock:
                counters["done"] += 1
        except GenerationStopped:
            return
        except Exception as e:
            log(f"  [!!!] Сцена {num} ({which}): не удалось сгенерировать картинку: {e}")
            with counters_lock:
                counters["failed"] += 1

    _run_tasks_parallel(tasks, process_one, max_parallel, log)

    log(f"\nГотово! Сгенерировано: {counters['done']}, уже было готово: {counters['skipped']}, "
        f"ошибок: {counters['failed']}")


# ---------------------------------------------------------------------------
# ВИДЕО (Veo) - через долгую операцию (submit -> poll -> download)
# ---------------------------------------------------------------------------

def _submit_video(prompt, image_data_uri, api_key, resolution):
    """image_data_uri - результат _image_to_data_uri() (строка вида
    'data:image/jpeg;base64,...'). Возвращает имя операции ('operations/xxx')
    для последующего опроса - сама генерация видео занимает время, ответ
    сразу не приходит."""
    _, _, b64_part = image_data_uri.partition("base64,")
    instance = {"prompt": prompt}
    if b64_part:
        instance["image"] = {"bytesBase64Encoded": b64_part, "mimeType": "image/jpeg"}
    payload = {
        "instances": [instance],
        "parameters": {
            "aspectRatio": VIDEO_ASPECT_RATIO,
            "durationSeconds": VIDEO_DURATION_SEC,
            "resolution": resolution,
        },
    }
    result = _api_request("POST", f"/models/{VIDEO_MODEL}:predictLongRunning", api_key, payload)
    return result["name"]


def _wait_for_video_operation(operation_name, api_key, on_progress=None, should_stop=None,
                               timeout_sec=VIDEO_POLL_TIMEOUT_SEC):
    started = time.time()
    while time.time() - started < timeout_sec:
        if should_stop and should_stop():
            raise GenerationStopped()
        result = _api_request("GET", f"/{operation_name}", api_key)
        if result.get("done"):
            if "error" in result:
                raise RuntimeError(f"Видео-задача завершилась с ошибкой: {result['error']}")
            return result
        if on_progress:
            on_progress()
        _sleep_interruptible(POLL_EVERY_SEC, should_stop)
    raise TimeoutError(f"Видео-задача не завершилась за {timeout_sec} секунд")


def _download_video(operation_result, api_key, save_path):
    samples = (operation_result.get("response", {})
                                .get("generateVideoResponse", {})
                                .get("generatedSamples", []))
    if not samples:
        raise RuntimeError(f"В ответе Google нет готового видео: {json.dumps(operation_result)[:500]}")
    video_uri = samples[0]["video"]["uri"]
    Path(save_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = ["curl", "-s", "-S", "-L", "--max-time", str(DOWNLOAD_TIMEOUT_SEC),
           "-H", f"x-goog-api-key: {api_key}",
           "-o", str(save_path), video_uri]
    result = _run_curl(cmd, DOWNLOAD_TIMEOUT_SEC, "Скачивание видео")
    if result.returncode != 0:
        raise RuntimeError(f"curl не смог скачать видео (код {result.returncode}): "
                            f"{result.stderr.strip()[-500:] or '(сообщение об ошибке пустое)'}")


def _submit_and_wait_video_with_retries(prompt, image_data_uri, api_key, resolution, log, label,
                                         should_stop=None):
    overall_started = time.time()  # с первой попытки - "прошло" в журнале считается по всей сцене
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        if should_stop and should_stop():
            raise GenerationStopped()
        try:
            operation_name = _submit_video(prompt, image_data_uri, api_key, resolution)
            progress_cb = _make_progress_logger(label, log, started=overall_started)
            return _wait_for_video_operation(operation_name, api_key, on_progress=progress_cb,
                                              should_stop=should_stop, timeout_sec=VIDEO_POLL_TIMEOUT_SEC)
        except GenerationStopped:
            raise
        except Exception as e:
            last_error = e
            log(f"  [!] {label}: попытка {attempt}/{MAX_RETRIES} не удалась ({e})")
            if attempt < MAX_RETRIES:
                _sleep_interruptible(RETRY_DELAY_SEC, should_stop)
    raise RuntimeError(f"{label}: не удалось после {MAX_RETRIES} попыток ({last_error})")


def generate_videos_from_upscaled(csv_path, upscaled_dir, output_dir, api_key,
                                   log=print, should_stop=None, max_parallel=DEFAULT_MAX_PARALLEL,
                                   resolution=VIDEO_RESOLUTION):
    """Сигнатура совпадает с royaltechno_generate.generate_videos_from_upscaled -
    см. её докстринг. Отличие только в провайдере (Google Veo вместо
    RoyalTechno/Veo)."""
    rows = read_rows(csv_path)
    os.makedirs(output_dir, exist_ok=True)

    counters = {"done": 0, "skipped": 0, "failed": 0}
    counters_lock = threading.Lock()

    tasks = []
    for row in rows:
        num = row.get("num", "").strip()
        animate_flag = row.get("animate", "").strip().upper()
        video_prompt = row.get("video_prompt", "").strip()
        if not num or animate_flag != "TRUE" or not video_prompt:
            continue
        for which in ("img1", "img2"):
            video_path = os.path.join(output_dir, f"{num}_{which}_video.mp4")
            tasks.append((num, which, video_prompt, video_path))

    def process_one(task):
        num, which, video_prompt, video_path = task
        if should_stop and should_stop():
            return
        if os.path.exists(video_path):
            if _looks_like_valid_video(video_path):
                with counters_lock:
                    counters["skipped"] += 1
                return
            log(f"  [!] Сцена {num} ({which}): найденный файл повреждён - перегенерирую: {video_path}")
            os.remove(video_path)

        upscaled_path = _find_upscaled_image(upscaled_dir, num, which)
        if not upscaled_path:
            log(f"  [!] Сцена {num} ({which}): нет апскейленной картинки в {upscaled_dir}, "
                f"пропускаю (сначала нужен апскейл на RunPod)")
            with counters_lock:
                counters["failed"] += 1
            return

        task_started = time.time()
        log(f"=== Сцена {num} ({which}) - оживляю через Google Veo {os.path.basename(upscaled_path)}...")
        try:
            data_uri = _image_to_data_uri(upscaled_path)
            operation_result = _submit_and_wait_video_with_retries(
                video_prompt, data_uri, api_key, resolution, log, f"видео {num}/{which}",
                should_stop=should_stop,
            )
            _download_video(operation_result, api_key, video_path)
            if not _looks_like_valid_video(video_path):
                raise RuntimeError("скачанное видео повреждено")
            log(f"  [+] Сцена {num} ({which}) готово за {format_elapsed(time.time() - task_started)}, "
                f"сохранено: {video_path}")
            with counters_lock:
                counters["done"] += 1
        except GenerationStopped:
            return
        except Exception as e:
            log(f"  [!!!] Сцена {num} ({which}): не удалось сгенерировать видео: {e}")
            with counters_lock:
                counters["failed"] += 1

    _run_tasks_parallel(tasks, process_one, max_parallel, log)

    log(f"\nГотово! Сгенерировано видео: {counters['done']}, уже было готово: {counters['skipped']}, "
        f"ошибок: {counters['failed']}")
