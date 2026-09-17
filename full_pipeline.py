"""
ПОЛНЫЙ КОНВЕЙЕР ОБРАБОТКИ БЛОКОВ СЦЕНАРИЯ (v2 - с детерминированным
разбиением на кадры)

Для каждого текстового файла блока (в указанной папке, по порядку):
  1. Если mp3 уже есть - берёт его длительность через ffprobe (не тратит
     деньги на Lumean заново). Иначе отправляет текст в Lumean (TTS) ->
     получает аудио и точную длительность.
  2. ДЕТЕРМИНИРОВАННО (без ИИ, бесплатно, через frame_segmenter.py) делит
     текст на кадры по границам предложений/пауз, с точным start/end -
     это гарантирует 100% покрытие текста без пропусков и без дублей.
  3. Отправляет уже готовые кадры (номер + текст) в OpenAI-совместимый
     API - модель дописывает ТОЛЬКО творческую часть (source, scene_ru,
     промты, ref_tags) для каждого кадра.
  4. Собирает финальный CSV, объединяя точный тайминг/текст (из шага 2)
     с творческой частью (из шага 3).
  5. Следующий блок продолжает нумерацию с последнего num предыдущего.

ИСПОЛЬЗОВАНИЕ:
    python3 full_pipeline.py --blocks-dir "тексты блоков" --library OBJECT_LIBRARY.md \
        --master VEO_3_МАСТЕР_ПРОМТ_КРЕАТИВ.txt --template-id 01a00ab2-3a8a-716c-b0f5-e205530b39d3 \
        --output-dir результаты --start-num 1

Файл frame_segmenter.py должен лежать в той же папке.

Перед запуском (в терминале, один раз в сессию):
    export LUMEAN_API_KEY="..."
    export OPENAI_API_KEY="..."
    export OPENAI_BASE_URL="..."   (если используется сервис-агрегатор)

ТРЕБОВАНИЯ:
    pip install openai
"""

import argparse
import csv
import io
import json
import os
import sys
import time
import http.client
import subprocess
import urllib.request
from pathlib import Path

try:
    from openai import OpenAI
except ImportError:
    print("Не установлена библиотека openai. Выполните: pip install openai")
    sys.exit(1)

from frame_segmenter import segment_text_into_frames, natural_sort_key_from_stem

LUMEAN_BASE = "https://api.lumean.app/api/public"
CSV_MODEL = "gpt-4o"
POLL_INTERVAL_SEC = 5
POLL_TIMEOUT_SEC = 600
MAX_FRAMES_PER_REQUEST = 25  # безопасный лимит кадров на один запрос к модели

CSV_COLUMNS = [
    "num", "start", "end", "duration", "animate", "source", "voiceover_ru",
    "scene_ru", "img_prompt_1", "img_prompt_2", "video_prompt", "ref_tags",
    "stock_query_1", "stock_query_2",
]


# ---------- вспомогательные функции ----------

def natural_sort_key(path):
    """Сортирует файлы по числу в начале имени (1, 2, ..., 10, 11), а не
    по алфавиту как текст. Файлы без числа (например "хук") идут ПЕРВЫМИ."""
    return natural_sort_key_from_stem(path.stem)


def read_file(path: str) -> str:
    p = Path(path)
    if not p.exists():
        print(f"ОШИБКА: файл не найден: {path}")
        sys.exit(1)
    return p.read_text(encoding="utf-8")


def lumean_request(method: str, path: str, api_key: str, body: dict | None = None,
                    max_retries: int = 3, retry_delay_sec: int = 5) -> dict:
    url = f"{LUMEAN_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None

    last_error = None
    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(url, data=data, method=method, headers={
            "X-API-KEY": api_key,
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8")
            print(f"ОШИБКА Lumean API ({e.code}): {error_body}")
            raise
        except (urllib.error.URLError, ConnectionError, TimeoutError,
                http.client.RemoteDisconnected) as e:
            last_error = e
            print(f"  [!] Сетевой сбой при обращении к Lumean ({e}), "
                  f"попытка {attempt}/{max_retries}...")
            if attempt < max_retries:
                time.sleep(retry_delay_sec)

    raise last_error


def get_existing_audio_duration(audio_path: Path):
    """Определяет длительность уже готового mp3-файла через ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    try:
        return round(float(result.stdout.strip()))
    except ValueError:
        return None


def create_tts_order(text: str, template_id: str, api_key: str) -> str:
    resp = lumean_request("POST", "/orders", api_key, {
        "template_id": template_id,
        "input_text": text,
    })
    return resp["data"]["id"]


def wait_for_order(order_id: str, api_key: str) -> dict:
    terminal = {"completed", "result_delivered", "failed", "compensated", "cancelled"}
    start = time.time()
    while True:
        resp = lumean_request("GET", f"/orders/{order_id}", api_key)
        order = resp["data"]
        status = order["status"]
        if status in terminal:
            return order
        if time.time() - start > POLL_TIMEOUT_SEC:
            print(f"ОШИБКА: озвучка не завершилась за {POLL_TIMEOUT_SEC} секунд")
            sys.exit(1)
        print(f"  ...озвучка в процессе ({status}), жду {POLL_INTERVAL_SEC} сек")
        time.sleep(POLL_INTERVAL_SEC)


def download_result_file(file_path: str, api_key: str, dest: Path):
    resp = lumean_request("POST", "/storage/url", api_key, {"path": file_path})
    url = resp["data"]["url"]
    urllib.request.urlretrieve(url, dest)


# ---------- творческая генерация (только источник/промты, не текст/тайминг) ----------

def strip_wrapping(text: str) -> str:
    text = text.strip()
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            candidate = part.strip()
            first_line, _, rest = candidate.partition("\n")
            if first_line.strip().lower() in ("csv", "text", "plaintext", ""):
                candidate = rest.strip()
            if ";" in candidate:
                return candidate
    return text


STYLE_ANCHOR_MARKER = "documentary realism"  # начало обязательного style anchor - должен быть в каждом промте
MIN_PROMPT_WORDS = 25  # ниже этого промт явно слишком короткий/абстрактный, не по формату


def _find_low_quality_nums(result: dict) -> set:
    """Находит кадры, у которых img_prompt_1/img_prompt_2/video_prompt явно
    нарушают формат мастер-промта: нет обязательного style anchor, или промт
    подозрительно короткий (несколько абстрактных слов вместо развёрнутого
    описания сцены по формуле [крупность+ракурс]+[объект]+[действие]+
    [окружение]+[реквизит]+[свет]+style anchor)."""
    bad_nums = set()
    for num, r in result.items():
        for field in ("img_prompt_1", "img_prompt_2", "video_prompt"):
            text = (r.get(field) or "").strip()
            if not text or text == "-":
                continue
            if STYLE_ANCHOR_MARKER not in text.lower() or len(text.split()) < MIN_PROMPT_WORDS:
                bad_nums.add(num)
                break
    return bad_nums


def generate_creative_fields(frames_group: list, library: str, master_prompt: str,
                              client, model: str, max_retries: int = 2):
    """Отправляет группу уже готовых кадров (num + voiceover_ru) модели,
    получает творческие поля для каждого. Возвращает словарь {num: {поля}}.
    Проверяет, что вернулись строки на ВСЕ переданные num, что нет
    повреждённых строк (лишние ';' внутри промта), и что промты не слишком
    короткие/абстрактные (есть style anchor, достаточно слов). При проблеме
    повторяет запрос, но ТОЛЬКО для тех кадров, которые реально не
    получились - а не для всей пачки заново. Это и дешевле (меньше токенов
    на повтор), и обычно даёт лучший результат - модели легче удержать
    внимание на маленьком фокусированном запросе, чем на большой пачке."""
    frames_by_num = {f["num"]: f for f in frames_group}
    expected_nums = set(frames_by_num.keys())
    result = {}
    pending = list(frames_group)  # кадры, которые нужно запросить в этом раунде

    for attempt in range(max_retries + 1):
        pending_nums = {f["num"] for f in pending}
        frames_list_text = "\n".join(
            f"[{f['num']}] {f['voiceover_ru']}" for f in pending
        )
        user_content = (
            f"Ниже список кадров с их номерами и готовым текстом. Заполни творческую "
            f"часть для КАЖДОГО из них, строго по формату из системной инструкции.\n\n"
            f"OBJECT_LIBRARY.md:\n{library}\n\n"
            f"КАДРЫ (номер и текст):\n{frames_list_text}"
        )
        response = client.chat.completions.create(
            model=model,
            max_completion_tokens=16384,
            messages=[
                {"role": "system", "content": master_prompt},
                {"role": "user", "content": user_content},
            ],
        )
        print(f"    finish_reason: {response.choices[0].finish_reason}, "
              f"токенов: {response.usage.total_tokens}")
        raw = strip_wrapping(response.choices[0].message.content)

        reader = csv.DictReader(io.StringIO(raw), delimiter=";", restkey="_EXTRA_")
        rows = list(reader)

        corrupted = [r for r in rows if r.get("_EXTRA_") or r.get("num") is None]
        round_result = {}
        for r in rows:
            try:
                num = int(r["num"])
            except (ValueError, TypeError, KeyError):
                continue
            if num in pending_nums:  # игнорируем строки не из этого раунда, мало ли что вернёт модель
                round_result[num] = r

        missing_nums = pending_nums - set(round_result.keys())
        low_quality_nums = _find_low_quality_nums(round_result)

        # принимаем все строки этого раунда, кроме тех, что оказались низкого качества
        for num, row in round_result.items():
            if num not in low_quality_nums:
                result[num] = row

        still_bad_nums = missing_nums | low_quality_nums
        if not still_bad_nums:
            return result

        problems = []
        if corrupted:
            problems.append(f"{len(corrupted)} повреждённых строк")
        if missing_nums:
            problems.append(f"нет ответа на кадры {sorted(missing_nums)}")
        if low_quality_nums:
            problems.append(f"слишком короткие/абстрактные промты у кадров {sorted(low_quality_nums)}")
        print(f"    [!] Проблема с ответом модели: {', '.join(problems)}")

        if attempt < max_retries:
            print(f"    Повторяю запрос ТОЛЬКО для проблемных кадров "
                  f"({len(still_bad_nums)} из {len(pending_nums)}), "
                  f"попытка {attempt + 2}/{max_retries + 1}...")
            pending = [frames_by_num[n] for n in still_bad_nums if n in frames_by_num]

    # после всех попыток - если для каких-то кадров вообще ничего не приняли
    # (например, они всё время оказывались "слишком короткими"), лучше взять
    # то, что получилось в последнем раунде, чем оставить кадр совсем пустым
    still_missing = expected_nums - set(result.keys())
    for num, row in round_result.items():
        if num in still_missing:
            result[num] = row

    print(f"    [!!!] После {max_retries + 1} попыток остались проблемы - "
          f"использую то, что получилось (недостающие кадры получат заглушку, "
          f"остальные проблемы смотри в журнале выше).")
    return result


def build_csv_rows(frames: list, creative_fields: dict) -> list:
    """Объединяет точный тайминг/текст (frames) с творческой частью
    (creative_fields, по num) в финальные строки CSV."""
    rows = []
    for f in frames:
        creative = creative_fields.get(f["num"], {})
        row = {
            "num": f["num"],
            "start": f["start"],
            "end": f["end"],
            "duration": f["duration"],
            "animate": "TRUE",
            "source": creative.get("source", "AI"),
            "voiceover_ru": f["voiceover_ru"],
            "scene_ru": creative.get("scene_ru", ""),
            "img_prompt_1": creative.get("img_prompt_1", ""),
            "img_prompt_2": creative.get("img_prompt_2", ""),
            "video_prompt": creative.get("video_prompt", ""),
            "ref_tags": creative.get("ref_tags", "-"),
            "stock_query_1": creative.get("stock_query_1", "-"),
            "stock_query_2": creative.get("stock_query_2", "-"),
        }
        rows.append(row)
    return rows


def write_csv(rows: list, output_path: Path):
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


# ---------- основной сценарий ----------

def main():
    parser = argparse.ArgumentParser(description="Полный конвейер: текст -> озвучка -> кадры -> CSV")
    parser.add_argument("--blocks-dir", required=True, help="Папка с текстовыми файлами блоков (.txt)")
    parser.add_argument("--library", required=True, help="Файл OBJECT_LIBRARY.md")
    parser.add_argument("--master", required=True, help="Файл с мастер-промтом (творческая часть)")
    parser.add_argument("--template-id", required=True, help="template_id голоса в Lumean")
    parser.add_argument("--output-dir", required=True, help="Куда сохранять аудио и CSV")
    parser.add_argument("--start-num", type=int, default=1, help="С какого num начинать первый блок")
    parser.add_argument("--only", type=str, default=None,
                         help="Обработать только блок с этим именем файла (без .txt) - для теста на одном блоке")
    parser.add_argument("--target-frame-sec", type=float, default=9.0,
                         help="Целевая длительность одного кадра в секундах (по умолчанию 9)")
    parser.add_argument("--csv-model", default=CSV_MODEL, help=f"Модель для творческой части (по умолчанию {CSV_MODEL})")
    args = parser.parse_args()

    lumean_key = os.environ.get("LUMEAN_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")
    openai_base_url = os.environ.get("OPENAI_BASE_URL")

    if not lumean_key:
        print("ОШИБКА: не задан LUMEAN_API_KEY")
        sys.exit(1)
    if not openai_key:
        print("ОШИБКА: не задан OPENAI_API_KEY")
        sys.exit(1)

    library = read_file(args.library)
    master_prompt = read_file(args.master)
    client = OpenAI(api_key=openai_key, base_url=openai_base_url) if openai_base_url \
        else OpenAI(api_key=openai_key)

    blocks_dir = Path(args.blocks_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    block_files = sorted(blocks_dir.glob("*.txt"), key=natural_sort_key)
    if args.only:
        block_files = [f for f in block_files if f.stem == args.only]
        if not block_files:
            print(f"ОШИБКА: блок '{args.only}' не найден в {blocks_dir}")
            sys.exit(1)
    if not block_files:
        print(f"ОШИБКА: в папке {blocks_dir} не найдено .txt файлов")
        sys.exit(1)

    print(f"Найдено блоков: {len(block_files)}")
    current_start_num = args.start_num

    for block_file in block_files:
        block_name = block_file.stem
        print(f"\n=== Блок: {block_name} (начинаю с num={current_start_num}) ===")

        voiceover_text = read_file(str(block_file))
        audio_dest = output_dir / f"{block_name}.mp3"

        if audio_dest.exists():
            duration_sec = get_existing_audio_duration(audio_dest)
            if duration_sec is not None:
                print(f"Озвучка уже есть: {audio_dest.name}, длительность: {duration_sec} сек "
                      f"(Lumean не трогаем, экономим)")
            else:
                print(f"[!] Аудио есть, но не удалось узнать длительность - озвучиваю заново")
                duration_sec = None
        else:
            duration_sec = None

        if duration_sec is None:
            print("Отправляю текст на озвучку в Lumean...")
            order_id = create_tts_order(voiceover_text, args.template_id, lumean_key)
            order = wait_for_order(order_id, lumean_key)

            if order["status"] not in ("completed", "result_delivered", "partially_completed"):
                print(f"ОШИБКА: озвучка не удалась, статус: {order['status']}")
                continue

            result = order.get("result") or {}
            files = result.get("files", [])
            if not files:
                print("ОШИБКА: в ответе нет готового аудиофайла")
                continue

            download_result_file(files[0], lumean_key, audio_dest)

            duration_ms = order.get("total_duration_ms")
            if duration_ms is None:
                print("ОШИБКА: не удалось получить длительность аудио")
                continue
            duration_sec = round(duration_ms / 1000)
            print(f"Озвучка готова: {audio_dest.name}, длительность: {duration_sec} сек")

        frames = segment_text_into_frames(
            voiceover_text, duration_sec, start_num=current_start_num,
            target_frame_sec=args.target_frame_sec,
        )
        if not frames:
            print("ОШИБКА: не удалось разбить текст на кадры (пустой текст?)")
            continue
        print(f"Разбито на {len(frames)} кадров (без участия ИИ, точное покрытие текста гарантировано)")

        all_creative = {}
        for i in range(0, len(frames), MAX_FRAMES_PER_REQUEST):
            group = frames[i:i + MAX_FRAMES_PER_REQUEST]
            print(f"  Генерирую творческую часть для кадров "
                  f"{group[0]['num']}-{group[-1]['num']} ({len(group)} шт)...")
            creative = generate_creative_fields(group, library, master_prompt, client, args.csv_model)
            all_creative.update(creative)

        rows = build_csv_rows(frames, all_creative)
        csv_dest = output_dir / f"{block_name}.csv"
        write_csv(rows, csv_dest)
        print(f"CSV сохранён: {csv_dest.name}")

        current_start_num = frames[-1]["num"] + 1
        print(f"Следующий блок начнётся с num={current_start_num}")

    print("\n=== Готово! Все блоки обработаны. ===")


if __name__ == "__main__":
    main()
