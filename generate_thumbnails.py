"""
Генерирует превью (thumbnail) для видео на основе анализа всего сценария:
  1. 10 вариантов текста заголовка (сохраняются в thumbnail_titles.txt,
     текст НЕ рисуется на картинках - только для справки, вставляешь
     сама в Figma)
  2. 10 вариантов картинки-фона превью через RoyalTechno API (nano-banana-2),
     в едином стиле канала, с фирменными янтарными глазами животного,
     затем апскейл через локальный ComfyUI до финального разрешения 2K
     (2560x1440)

ИСПОЛЬЗОВАНИЕ:
    python3 generate_thumbnails.py --blocks-dir "тексты блоков" --library OBJECT_LIBRARY.md \
        --titles-master MASTER_ПРОМТ_THUMBNAIL_TITLES.txt \
        --images-master MASTER_ПРОМТ_THUMBNAIL_IMAGES.txt \
        --output-dir превью

Перед запуском:
    export OPENAI_API_KEY="..."
    export OPENAI_BASE_URL="..."   (если нужен сервис-агрегатор)
    export ROYALTECHNO_API_KEY="..."

ТРЕБОВАНИЯ:
    pip install openai
"""

import argparse
import copy
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    from openai import OpenAI
except ImportError:
    print("Не установлена библиотека openai. Выполните: pip install openai")
    sys.exit(1)

TEXT_MODEL = "gpt-4o"

# Проверенный вручную шаблон, который дал нужный результат (см. чат с пользователем).
# Модель заполняет только {animal}, {texture_detail} и {location_detail} -
# вся остальная формулировка зафиксирована и не меняется.
THUMBNAIL_TEMPLATE = (
    "{animal}, extreme close-up portrait filling the right two-thirds of the "
    "frame, intensely glowing saturated amber orange eyes as the brightest "
    "and most vivid point in the entire image, strong luminous contrast "
    "against the dark fur, pupils sharp and catching light like embers, "
    "intense direct gaze into camera, {texture_detail}, cropped tight at "
    "the shoulders so the animal dominates the composition, {location_detail}, "
    "the entire left third of the frame rendered significantly darker and "
    "underexposed with a heavy black gradient vignette for text overlay, "
    "documentary realism, cinematic side lighting focused on the animal only, "
    "muted cold color palette, 16:9, photorealistic, no text, no watermark"
)

# RoyalTechno API (те же настройки, что в generate_via_api_and_upscale.py)
API_BASE = "https://api.royaltechno.cc/v1"
IMAGE_MODEL = "nano-banana-2"
IMAGE_QUALITY = "auto"
IMAGE_ASPECT_RATIO = "landscape"

FINAL_WIDTH = 2560
FINAL_HEIGHT = 1440

POLL_EVERY_SEC = 3
POLL_TIMEOUT_SEC = 300
MAX_RETRIES = 3
RETRY_DELAY_SEC = 10



def natural_sort_key(path):
    """Сортирует файлы по числу в начале имени (1, 2, ..., 10, 11), а не
    по алфавиту как текст (где "10" встаёт перед "2"). Файлы без числа
    в начале (например "хук") идут ПЕРВЫМИ, как вступление перед блоком 1."""
    match = re.match(r"^(\d+)", path.stem)
    if match:
        return (0, int(match.group(1)), path.stem)
    return (-1, 0, path.stem)  # "хук" и подобные - идут ПЕРВЫМИ, как вступление


def read_file(path) -> str:
    p = Path(path)
    if not p.exists():
        print(f"ОШИБКА: файл не найден: {path}")
        sys.exit(1)
    return p.read_text(encoding="utf-8")


def strip_wrapping(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


# ---------- шаг 1: тексты заголовков ----------

def generate_titles(full_script: str, master_prompt: str, client, model) -> list:
    print("Генерирую 10 вариантов текста заголовка...")
    response = client.chat.completions.create(
        model=model,
        max_tokens=2000,
        messages=[
            {"role": "system", "content": master_prompt},
            {"role": "user", "content": f"ПОЛНЫЙ ТЕКСТ СЦЕНАРИЯ:\n\n{full_script}"},
        ],
    )
    text = strip_wrapping(response.choices[0].message.content)
    titles = [line.strip() for line in text.split("\n") if line.strip()]
    return titles


# ---------- шаг 2: промты для картинок ----------

def generate_image_prompts(full_script: str, library: str, master_prompt: str, client, model):
    print("Анализирую сценарий и генерирую 10 вариантов сцены для превью...")
    response = client.chat.completions.create(
        model=model,
        max_tokens=4000,
        messages=[
            {"role": "system", "content": master_prompt},
            {"role": "user", "content": (
                f"OBJECT_LIBRARY.md:\n{library}\n\n"
                f"ПОЛНЫЙ ТЕКСТ СЦЕНАРИЯ:\n\n{full_script}"
            )},
        ],
    )
    text = strip_wrapping(response.choices[0].message.content)
    lines = text.split("\n")

    animal_line = next((l for l in lines if l.lower().startswith("animal:")), "")
    texture_line = next((l for l in lines if l.lower().startswith("texture:")), "")
    animal = animal_line.split(":", 1)[1].strip() if ":" in animal_line else "wild animal"
    texture_detail = texture_line.split(":", 1)[1].strip() if ":" in texture_line else \
        "detailed fur rendered in sharp focus"

    # Всё после строк animal:/texture: и пустой строки - блоки локаций через "---"
    rest_start = text.find("\n\n")
    rest = text[rest_start:].strip() if rest_start != -1 else text
    location_details = [p.strip() for p in rest.split("---") if p.strip()]

    final_prompts = [
        THUMBNAIL_TEMPLATE.format(animal=animal, texture_detail=texture_detail,
                                   location_detail=loc)
        for loc in location_details
    ]
    main_animal_display = f"Главное животное: {animal}"
    return main_animal_display, final_prompts


# ---------- RoyalTechno API (та же логика, что в generate_via_api_and_upscale.py) ----------

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


def wait_for_job(job_id):
    started = time.time()
    while time.time() - started < POLL_TIMEOUT_SEC:
        result = _api_request("GET", f"/jobs/{job_id}")
        status = result.get("status")
        if status == "succeeded":
            print()
            return result
        if status == "failed":
            print()
            raise RuntimeError(f"Задача {job_id} завершилась с ошибкой: {result}")
        print(".", end="", flush=True)
        time.sleep(POLL_EVERY_SEC)
    print()
    raise TimeoutError(f"Задача {job_id} не завершилась за {POLL_TIMEOUT_SEC} секунд")


def download_image(url, save_path):
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0.0"})
    with urllib.request.urlopen(req) as resp:
        data = resp.read()
    with open(save_path, "wb") as f:
        f.write(data)


# ---------- ЛОКАЛЬНЫЙ COMFYUI - АПСКЕЙЛ ДО 2K ----------

def load_upscale_template(path):
    if not os.path.exists(path):
        print(f"[!] Не найден файл схемы апскейла: {path}")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def queue_upscale(comfyui_url, template, input_filename, output_prefix, width, height):
    wf = copy.deepcopy(template)
    wf["1"]["inputs"]["image"] = input_filename
    wf["12"]["inputs"]["width"] = width
    wf["12"]["inputs"]["height"] = height
    wf["9"]["inputs"]["filename_prefix"] = output_prefix

    payload = json.dumps({"prompt": wf}).encode("utf-8")
    req = urllib.request.Request(
        f"{comfyui_url}/prompt", data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def process_thumbnail(index, prompt, comfyui_url, comfyui_input_dir, upscale_template):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"\n=== Превью {index} (попытка {attempt}/{MAX_RETRIES}) ===")
            print(f"Промпт (первые 150 символов): {prompt[:150]}...")

            job_id = submit_image_job(prompt)
            print(f"[+] Задача отправлена, id: {job_id}")

            result = wait_for_job(job_id)
            image_url = result["output"]["url"]
            cost = result.get("cost_usd_cents", 0)
            print(f"[+] Готово. Стоимость: {cost} центов.")

            raw_filename = f"thumb_{index:02d}_raw.jpg"
            raw_path = os.path.join(comfyui_input_dir, raw_filename)
            download_image(image_url, raw_path)

            upscale_result = queue_upscale(
                comfyui_url, upscale_template, raw_filename,
                f"thumb_{index:02d}", FINAL_WIDTH, FINAL_HEIGHT,
            )
            print(f"[+] Отправлено на апскейл до {FINAL_WIDTH}x{FINAL_HEIGHT}, "
                  f"id задачи ComfyUI: {upscale_result.get('prompt_id')}")
            return True

        except Exception as e:
            print(f"[!] Ошибка на превью {index}: {e}")
            if attempt < MAX_RETRIES:
                print(f"[!] Жду {RETRY_DELAY_SEC} сек перед следующей попыткой...")
                time.sleep(RETRY_DELAY_SEC)

    print(f"[!!!] Превью {index} не удалось сгенерировать после {MAX_RETRIES} попыток.")
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--blocks-dir", required=True, help="Папка с текстами всех блоков сценария (.txt)")
    parser.add_argument("--library", required=True, help="Файл OBJECT_LIBRARY.md")
    parser.add_argument("--titles-master", required=True, help="Мастер-промт для генерации заголовков")
    parser.add_argument("--images-master", required=True, help="Мастер-промт для генерации промтов картинок")
    parser.add_argument("--output-dir", required=True, help="Куда сохранить thumbnail_titles.txt и промты")
    parser.add_argument("--upscale-workflow", default="workflow_upscale_only.json",
                         help="Путь к схеме апскейла ComfyUI (по умолчанию workflow_upscale_only.json рядом со скриптом)")
    parser.add_argument("--comfyui-url", default="http://127.0.0.1:8188")
    parser.add_argument("--comfyui-input-dir", default=None,
                         help="Папка ComfyUI/input (по умолчанию ../input относительно скрипта)")
    parser.add_argument("--run", action="store_true", help="Реально генерировать картинки (без флага - только текст заголовков и промтов)")
    parser.add_argument("--limit", type=int, default=None, help="Сгенерировать только первые N картинок (для теста)")
    parser.add_argument("--text-model", default=TEXT_MODEL)
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("ОШИБКА: не задан OPENAI_API_KEY")
        sys.exit(1)
    base_url = os.environ.get("OPENAI_BASE_URL")
    client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)

    library = read_file(args.library)
    titles_master = read_file(args.titles_master)
    images_master = read_file(args.images_master)

    blocks_dir = Path(args.blocks_dir)
    block_files = sorted(blocks_dir.glob("*.txt"), key=natural_sort_key)
    if not block_files:
        print(f"ОШИБКА: в папке {blocks_dir} не найдено .txt файлов")
        sys.exit(1)

    full_script = "\n\n".join(
        f"=== {f.stem} ===\n{read_file(str(f))}" for f in block_files
    )
    print(f"Сценарий собран из {len(block_files)} блоков, {len(full_script)} символов")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Шаг 1: заголовки
    titles = generate_titles(full_script, titles_master, client, args.text_model)
    titles_path = output_dir / "thumbnail_titles.txt"
    titles_path.write_text("\n".join(titles), encoding="utf-8")
    print(f"Сохранено {len(titles)} вариантов заголовка: {titles_path}")
    for i, t in enumerate(titles, 1):
        print(f"  {i}. {t.replace('|', ' / ')}")

    # Шаг 2: промты для картинок
    main_animal, image_prompts = generate_image_prompts(full_script, library, images_master, client, args.text_model)
    print(f"\n{main_animal}")
    print(f"Сгенерировано {len(image_prompts)} промтов для картинок")

    prompts_path = output_dir / "thumbnail_image_prompts.txt"
    prompts_path.write_text(
        main_animal + "\n\n" + "\n---\n".join(image_prompts), encoding="utf-8"
    )
    print(f"Промты сохранены: {prompts_path}")

    if not args.run:
        print("\n[i] Сухой прогон - картинки не генерировались. Добавь --run для реальной генерации.")
        return

    # Шаг 3: генерация картинок + апскейл
    script_dir = Path(__file__).resolve().parent
    comfyui_input_dir = args.comfyui_input_dir or str(script_dir / ".." / "input")
    os.makedirs(comfyui_input_dir, exist_ok=True)

    upscale_workflow_path = args.upscale_workflow
    if not os.path.isabs(upscale_workflow_path):
        upscale_workflow_path = str(script_dir / upscale_workflow_path)
    upscale_template = load_upscale_template(upscale_workflow_path)

    if args.limit:
        image_prompts_to_run = image_prompts[:args.limit]
        print(f"[i] --limit {args.limit}: генерирую только первые {len(image_prompts_to_run)} картинок")
    else:
        image_prompts_to_run = image_prompts

    failed = []
    for i, prompt in enumerate(image_prompts_to_run, 1):
        success = process_thumbnail(i, prompt, args.comfyui_url, comfyui_input_dir, upscale_template)
        if not success:
            failed.append(i)
        time.sleep(1)

    print(f"\n=== Готово! Картинки появятся в ComfyUI/output как thumb_01... thumb_{len(image_prompts):02d} "
          f"после апскейла ({FINAL_WIDTH}x{FINAL_HEIGHT}). ===")
    if failed:
        print(f"Не удалось сгенерировать превью: {failed}. Можно перезапустить с --run.")


if __name__ == "__main__":
    main()
