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
import difflib
import io
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

API_BASE = "https://api.royaltechno.cc/v1"
IMAGE_MODEL = "nano-banana-2"  # или "nano-banana-pro" - выбирается в Настройках
IMAGE_QUALITY = "auto"
IMAGE_ASPECT_RATIO = "landscape"

VIDEO_MODEL = "veo-3.1"
VIDEO_DURATION_SEC = 8
VIDEO_RESOLUTION = "1080p"

POLL_EVERY_SEC = 3
POLL_TIMEOUT_SEC = 300       # картинки обычно готовы быстро
VIDEO_POLL_TIMEOUT_SEC = 1200  # видео (veo-3.1) при задержках на стороне RoyalTechno может идти намного дольше 5 минут
MAX_RETRIES = 3
RETRY_DELAY_SEC = 10

# Тайм-ауты на сетевые запросы. Без них, если сервер RoyalTechno завис
# или соединение оборвалось "молча" (без ошибки), curl будет ждать
# ответа БЕСКОНЕЧНО - задача не завершится и не покажет ошибку, просто
# зависнет навсегда. С тайм-аутом зависание превращается в обычную
# ошибку, на которую сработает повторная попытка (retry).
REQUEST_TIMEOUT_SEC = 60
DOWNLOAD_TIMEOUT_SEC = 180

# Сколько сцен генерировать одновременно - по умолчанию 3, как позволяет
# обычный тариф RoyalTechno (столько же потоков было в старой очереди
# генерации через RunPod, кнопка 4). Если тариф уже, поменяй значение.
DEFAULT_MAX_PARALLEL = 3

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


def expand_tags(prompt_text, ref_tags_field, library, log=None):
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
                if log:
                    log(f"  [i] Тег '{tag}' не найден дословно в OBJECT_LIBRARY.md - "
                        f"использую похожий тег '{closest}' (возможно, опечатка)")
        if not obj:
            if log:
                log(f"  [i] Тег '{tag}' не найден в OBJECT_LIBRARY.md - пропускаю "
                    f"(нормально, если объект появляется в сценарии только 1 раз; "
                    f"если появляется несколько раз - библиотеку стоит перегенерировать/дополнить)")
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

def _run_curl(cmd, timeout_sec, what):
    """Запускает curl как отдельную программу (вместо встроенного в Python
    механизма HTTPS-запросов) и возвращает завершённый процесс. На части
    компьютеров (Windows) встроенный в Python способ подключения по HTTPS
    почему-то обрывается сервером RoyalTechno (Cloudflare), хотя ТОЧНО
    ТАКОЙ ЖЕ запрос через curl проходит без проблем - поэтому вместо
    "изобретения" своего HTTPS-подключения программа просто пользуется
    curl, который есть в Windows 10/11 "из коробки"."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec + 10)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"{what} не ответил за {timeout_sec} сек") from e
    except FileNotFoundError as e:
        raise RuntimeError(
            "Не найдена программа 'curl' на этом компьютере (обычно она уже "
            "встроена в Windows 10/11 - проверь командой 'curl -version' в "
            "командной строке)") from e


def _api_request(method, path, api_key, payload=None):
    url = f"{API_BASE}{path}"
    cmd = ["curl", "-s", "-S", "-X", method, url,
           "--max-time", str(REQUEST_TIMEOUT_SEC),
           "-H", f"Authorization: Bearer {api_key}",
           "-H", "Content-Type: application/json",
           "-w", "\n%{http_code}"]

    tmp_path = None
    if payload is not None:
        # передаём тело запроса через временный файл, а не прямо в команду -
        # у base64-картинок в видео-запросах тело может весить мегабайты,
        # а это больше, чем разрешено передавать одним аргументом команды
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tmp:
            json.dump(payload, tmp)
            tmp_path = tmp.name
        cmd += ["-d", f"@{tmp_path}"]

    try:
        result = _run_curl(cmd, REQUEST_TIMEOUT_SEC, f"RoyalTechno ({method} {path})")
    finally:
        if tmp_path:
            os.unlink(tmp_path)

    if result.returncode != 0:
        raise RuntimeError(f"curl не смог связаться с RoyalTechno (код {result.returncode}): "
                            f"{result.stderr.strip()[-500:]}")

    body, _, status_code = result.stdout.rpartition("\n")
    try:
        status = int(status_code)
    except ValueError:
        raise RuntimeError(f"Не удалось разобрать ответ RoyalTechno: {result.stdout[:500]}")

    if status >= 400:
        raise RuntimeError(f"Ошибка RoyalTechno API {status}: {body}")

    return json.loads(body)


def submit_image_job(prompt, api_key, model=IMAGE_MODEL):
    payload = {
        "model": model,
        "input": {
            "prompt": prompt,
            "aspect_ratio": IMAGE_ASPECT_RATIO,
            "resolution": IMAGE_QUALITY,
        },
    }
    result = _api_request("POST", "/jobs", api_key, payload)
    return result["id"]


def submit_video_job(prompt, start_image_source, api_key, resolution=VIDEO_RESOLUTION):
    """start_image_source - либо обычная ссылка (str, начинается с http),
    либо инлайн data URI (str, уже начинается с 'data:'). resolution -
    "1080p" или "720p" - если у RoyalTechno проблемы с апскейлом до 1080p
    (задержки/таймауты), поддержка рекомендует временно генерить в 720p."""
    payload = {
        "model": VIDEO_MODEL,
        "input": {
            "prompt": prompt,
            "start_image_url": start_image_source,
            "duration_sec": VIDEO_DURATION_SEC,
            "resolution": resolution,
        },
    }
    result = _api_request("POST", "/jobs", api_key, payload)
    return result["id"]


class GenerationStopped(Exception):
    """Пользователь нажал 'Остановить генерацию' - прерываем немедленно,
    не дожидаясь окончания текущей попытки/паузы между попытками."""


def _sleep_interruptible(total_sec, should_stop):
    """Спит total_sec секунд, но каждые 0.5 сек проверяет should_stop() и
    выходит досрочно, если пользователь нажал 'Остановить генерацию'."""
    if not should_stop:
        time.sleep(total_sec)
        return
    remaining = total_sec
    step = 0.5
    while remaining > 0:
        if should_stop():
            raise GenerationStopped()
        time.sleep(min(step, remaining))
        remaining -= step


PROGRESS_LOG_INTERVAL_SEC = 25


def _make_progress_logger(label, log, interval_sec=PROGRESS_LOG_INTERVAL_SEC):
    """Периодически пишет в журнал 'ещё жду ответ', пока задача выполняется
    на стороне RoyalTechno, - чтобы долгое молчание в журнале (нормальное
    при реальной генерации видео/картинки) не выглядело как зависание."""
    last_logged = [time.time()]

    def _progress():
        now = time.time()
        if now - last_logged[0] >= interval_sec:
            last_logged[0] = now
            log(f"  [i] {label}: всё ещё жду ответ от RoyalTechno...")

    return _progress


def wait_for_job(job_id, api_key, on_progress=None, should_stop=None, timeout_sec=POLL_TIMEOUT_SEC):
    started = time.time()
    while time.time() - started < timeout_sec:
        if should_stop and should_stop():
            raise GenerationStopped()
        result = _api_request("GET", f"/jobs/{job_id}", api_key)
        status = result.get("status")
        if status == "succeeded":
            return result
        if status == "failed":
            raise RuntimeError(f"Задача {job_id} завершилась с ошибкой: {result}")
        if on_progress:
            on_progress()
        _sleep_interruptible(POLL_EVERY_SEC, should_stop)
    raise TimeoutError(f"Задача {job_id} не завершилась за {timeout_sec} секунд")


def _submit_and_wait_with_retries(submit_fn, api_key, log, label, should_stop=None,
                                   timeout_sec=POLL_TIMEOUT_SEC):
    """Обёртка с повторными попытками вокруг отправки+ожидания одной
    задачи - сетевые сбои/временные ошибки API не должны сразу обрывать
    всю пачку. should_stop проверяется перед каждой попыткой и во время
    ожидания/пауз, чтобы 'Остановить генерацию' прерывало сразу, а не
    через 10-30 секунд. timeout_sec - сколько ждать одну задачу, прежде
    чем считать попытку неудавшейся и заказывать генерацию заново (для
    видео нужно намного больше, чем для картинок - см. VIDEO_POLL_TIMEOUT_SEC)."""
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        if should_stop and should_stop():
            raise GenerationStopped()
        try:
            job_id = submit_fn()
            progress_cb = _make_progress_logger(label, log)
            result = wait_for_job(job_id, api_key, on_progress=progress_cb, should_stop=should_stop,
                                   timeout_sec=timeout_sec)
            return result
        except GenerationStopped:
            raise
        except Exception as e:
            last_error = e
            log(f"  [!] {label}: попытка {attempt}/{MAX_RETRIES} не удалась ({e})")
            if attempt < MAX_RETRIES:
                _sleep_interruptible(RETRY_DELAY_SEC, should_stop)
    raise RuntimeError(f"{label}: не удалось после {MAX_RETRIES} попыток ({last_error})")


def download_url(url, save_path):
    Path(save_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = ["curl", "-s", "-S", "-L", "--max-time", str(DOWNLOAD_TIMEOUT_SEC), "-o", str(save_path), url]
    result = _run_curl(cmd, DOWNLOAD_TIMEOUT_SEC, "Скачивание")
    if result.returncode != 0:
        raise RuntimeError(f"curl не смог скачать файл (код {result.returncode}): "
                            f"{result.stderr.strip()[-500:] or '(сообщение об ошибке пустое)'} - {url}")


def _looks_like_valid_image(path):
    """Проверяет, что картинка скачалась ПОЛНОСТЬЮ, а не оборвалась на
    середине (при обрыве соединения curl иногда всё равно сохраняет уже
    полученный кусок файла - такой файл физически существует, но открыть
    его целиком нельзя, обычно видно как серая заливка вместо низа
    картинки)."""
    try:
        from PIL import Image
    except ImportError:
        return os.path.getsize(path) > 10_000  # нет Pillow - хотя бы грубая проверка размера
    try:
        with Image.open(path) as img:
            # .verify() слишком "мягкая" для JPEG - может пропустить
            # оборванный файл, не заметив, что не хватает конца данных.
            # .load() заставляет Pillow реально разобрать ВСЕ пиксели,
            # поэтому обрыв обязательно вылезет ошибкой.
            img.load()
        return True
    except Exception:
        return False


def _looks_like_valid_video(path):
    """Грубая проверка на оборванную закачку видео - настоящий 8-секундный
    ролик весит куда больше, чем то, что успевает долететь при обрыве
    соединения на середине."""
    try:
        return os.path.getsize(path) > 100_000
    except OSError:
        return False


def download_url_with_retries(url, save_path, log, label, verify_fn=None, should_stop=None):
    """Картинка/видео уже сгенерированы и оплачены к этому моменту - если
    падает именно скачивание (а не сама генерация), нет смысла заказывать
    генерацию заново, дешевле и быстрее просто повторить скачивание того
    же самого готового файла несколько раз."""
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        if should_stop and should_stop():
            raise GenerationStopped()
        try:
            download_url(url, save_path)
            if verify_fn and not verify_fn(save_path):
                raise RuntimeError("скачанный файл повреждён/обрезан (закачка оборвалась на середине)")
            return
        except Exception as e:
            last_error = e
            if os.path.exists(save_path):
                os.remove(save_path)  # не оставляем битый файл - иначе программа решит, что всё уже готово
            log(f"  [!] {label}: скачивание, попытка {attempt}/{MAX_RETRIES} не удалась ({e})")
            if attempt < MAX_RETRIES:
                _sleep_interruptible(RETRY_DELAY_SEC, should_stop)
    raise RuntimeError(f"{label}: скачивание не удалось после {MAX_RETRIES} попыток ({last_error})")


def _run_tasks_parallel(tasks, worker_fn, max_parallel, log):
    """Выполняет worker_fn(task) для каждой задачи из tasks, до max_parallel
    штук одновременно (как очередь в 3 потока, которая раньше была у
    кнопки 4 через RunPod). worker_fn сам решает, что считать "готово/
    пропущено/ошибка" и пишет об этом в log - здесь только параллельный
    запуск, без подсчёта результатов."""
    if not tasks:
        return
    log(f"[i] Запускаю с параллелизмом {max_parallel} "
        f"(как позволяет тариф выбранного провайдера)...")
    with ThreadPoolExecutor(max_workers=max_parallel) as executor:
        list(executor.map(worker_fn, tasks))


# ---------------------------------------------------------------------------
# ШАГ 1: ГЕНЕРАЦИЯ СЫРЫХ КАРТИНОК (без RunPod)
# ---------------------------------------------------------------------------

def generate_images(csv_path, library_path, output_dir, api_key, log=print,
                     limit=None, should_stop=None, max_parallel=DEFAULT_MAX_PARALLEL,
                     image_model=IMAGE_MODEL):
    """Для каждой сцены в CSV отправляет img_prompt_1 и img_prompt_2 в
    RoyalTechno, скачивает сырые (не апскейленные) картинки в output_dir
    с именами {num}_{which}_raw.jpg - такое же имя, которое ожидает
    апскейл на RunPod. Уже готовые файлы не перегенерируются. До
    max_parallel сцен обрабатываются одновременно (по умолчанию 3, как
    позволяет обычный тариф RoyalTechno).

    image_model - "nano-banana-2" (по умолчанию) или "nano-banana-pro"
    (выбирается в Настройках).

    should_stop - необязательная функция без аргументов, возвращающая
    True, если нужно прервать процесс (для кнопки отмены)."""
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
        # картинки-инфографику (source=AI_INFOGRAPHIC) генерирует не
        # RoyalTechno, а OpenAI (заметно лучше рисует читаемый текст) -
        # это происходит отдельно, на RunPod, во время шага B (апскейл),
        # здесь их только пропускаем, чтобы не тратить деньги RoyalTechno
        # на заведомо не тот результат
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
            log(f"  [!] Сцена {num} ({which}): найденный файл повреждён (закачка когда-то "
                f"оборвалась) - перегенерирую: {raw_path}")
            os.remove(raw_path)

        full_prompt = expand_tags(base_prompt, ref_tags, library, log=log)
        log(f"=== Сцена {num} ({which}) - запрос картинки в RoyalTechno...")
        try:
            result = _submit_and_wait_with_retries(
                lambda: submit_image_job(full_prompt, api_key, model=image_model),
                api_key, log, f"картинка {num}/{which}",
                should_stop=should_stop,
            )
            image_url = result["output"]["url"]
            cost = result.get("cost_usd_cents", 0)
            download_url_with_retries(image_url, raw_path, log, f"картинка {num}/{which}",
                                       verify_fn=_looks_like_valid_image, should_stop=should_stop)
            log(f"  [+] Сцена {num} ({which}) готово, стоимость {cost} центов, "
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
                                   log=print, should_stop=None, max_parallel=DEFAULT_MAX_PARALLEL,
                                   resolution=VIDEO_RESOLUTION):
    """Для каждой сцены с animate=TRUE берёт уже апскейленную картинку
    (из upscaled_dir, скачанную с RunPod после апскейла), отправляет её
    в RoyalTechno/Veo как инлайн-картинку (без отдельной загрузки куда-
    либо) и скачивает готовое видео в output_dir с именем
    {num}_{which}_video.mp4 - таким же, какое ожидает финальная сборка
    видео (кнопка 12). До max_parallel сцен обрабатываются одновременно."""
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
            log(f"  [!] Сцена {num} ({which}): найденный файл повреждён (закачка когда-то "
                f"оборвалась) - перегенерирую: {video_path}")
            os.remove(video_path)

        upscaled_path = _find_upscaled_image(upscaled_dir, num, which)
        if not upscaled_path:
            log(f"  [!] Сцена {num} ({which}): нет апскейленной картинки в {upscaled_dir}, "
                f"пропускаю (сначала нужен апскейл на RunPod)")
            with counters_lock:
                counters["failed"] += 1
            return

        log(f"=== Сцена {num} ({which}) - оживляю {os.path.basename(upscaled_path)}...")
        try:
            data_uri = _image_to_data_uri(upscaled_path)
            result = _submit_and_wait_with_retries(
                lambda: submit_video_job(video_prompt, data_uri, api_key, resolution),
                api_key, log, f"видео {num}/{which}",
                should_stop=should_stop, timeout_sec=VIDEO_POLL_TIMEOUT_SEC,
            )
            video_url = result["output"]["url"]
            cost = result.get("cost_usd_cents", 0)
            download_url_with_retries(video_url, video_path, log, f"видео {num}/{which}",
                                       verify_fn=_looks_like_valid_video, should_stop=should_stop)
            log(f"  [+] Сцена {num} ({which}) готово, стоимость {cost} центов, "
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
