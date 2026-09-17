"""
Проходит по всем CSV-блокам сценария ПО ПОРЯДКУ и для каждого решает:
  - если блок в списке "сломанных" (--broken) - перегенерирует его целиком
    через API (используя реальную длительность аудио и правильный start_num),
  - если блок в порядке - просто ПЕРЕНУМЕРОВЫВАЕТ колонку num (сдвигает
    на нужное число, без обращения к API - бесплатно и мгновенно),
чтобы в итоге вся последовательность num была сквозной и без разрывов/
задвоений, независимо от того, какие блоки чинились.

ИСПОЛЬЗОВАНИЕ:
    python3 fix_and_renumber_pipeline.py --dir результаты --texts-dir "тексты блоков" \
        --library OBJECT_LIBRARY.md --master VEO_2_МАСТЕР_ПРОМТ_ВСЕ_ВИДЕО.txt \
        --broken 3_block,4_block,8_block,9_block --start-num 1

Перед запуском:
    export OPENAI_API_KEY="..."
    export OPENAI_BASE_URL="..."   (если нужен сервис-агрегатор)

Скрипт сам определяет реальную длительность звука через ffprobe по
одноимённому .mp3 в той же папке. Если ffprobe недоступен или mp3 не
найден для какого-то сломанного блока - скрипт остановится и попросит
задать длительность вручную через --manual-durations "3_block=428".

ВАЖНО: скрипт перезаписывает CSV-файлы в --dir. Перед запуском стоит
сохранить копию папки, если есть сомнения.
"""

import argparse
import re
import csv
import io
import os
import subprocess
import sys
from pathlib import Path

try:
    from openai import OpenAI
except ImportError:
    print("Не установлена библиотека openai. Выполните: pip install openai")
    sys.exit(1)

MODEL = "gpt-4o"
MAX_SECONDS_PER_REQUEST = 220


# ---------- вспомогательные функции ----------


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


def get_audio_duration_ffprobe(mp3_path: Path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(mp3_path)],
            capture_output=True, text=True, check=True,
        )
        return round(float(result.stdout.strip()))
    except Exception:
        return None


def read_csv_rows(csv_path: Path):
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        fieldnames = reader.fieldnames
        rows = list(reader)
    return fieldnames, rows


def write_csv_rows(csv_path: Path, fieldnames, rows):
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def renumber_csv(csv_path: Path, new_start_num: int):
    """Сдвигает колонку num так, чтобы первая строка была new_start_num.
    Возвращает (последний_num, is_corrupted). Если в файле есть строки с
    лишними полями (случайная ';' внутри промта сломала структуру CSV) -
    возвращает is_corrupted=True и не трогает файл."""
    fieldnames, rows = read_csv_rows(csv_path)
    if not rows:
        return new_start_num - 1, False

    corrupted_rows = [i for i, row in enumerate(rows) if None in row]
    if corrupted_rows:
        print(f"  [!] ПОВРЕЖДЁН: в {csv_path.name} есть строки с лишними полями "
              f"(вероятно, случайная ';' внутри промта сломала структуру) - "
              f"строки: {corrupted_rows[:5]}{'...' if len(corrupted_rows) > 5 else ''}")
        return None, True

    try:
        original_first = int(rows[0]["num"])
    except (ValueError, KeyError, TypeError):
        print(f"  [!] ПОВРЕЖДЁН: не удалось прочитать num в первой строке {csv_path.name}")
        return None, True

    bad_num_rows = []
    offset = new_start_num - original_first
    for i, row in enumerate(rows):
        try:
            row["num"] = str(int(row["num"]) + offset)
        except (ValueError, KeyError, TypeError):
            bad_num_rows.append(i)

    if bad_num_rows:
        print(f"  [!] ПОВРЕЖДЁН: строки с нечитаемым num в {csv_path.name}: {bad_num_rows[:5]}")
        return None, True

    write_csv_rows(csv_path, fieldnames, rows)
    last_num = int(rows[-1]["num"])
    return last_num, False


# ---------- генерация через API (та же логика, что в generate_csv_from_text.py) ----------

def strip_markdown_fence(text: str) -> str:
    text = text.strip()
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            candidate = part.strip()
            first_line, _, rest = candidate.partition("\n")
            if first_line.strip().lower() in ("csv", "text", "plaintext", ""):
                candidate = rest.strip()
            if ";" in candidate and "num" in candidate.lower():
                return candidate
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("num;"):
            return "\n".join(lines[i:]).strip()
    return text


def get_max_num_from_text(csv_text: str):
    reader = csv.DictReader(io.StringIO(csv_text), delimiter=";")
    max_num = None
    for row in reader:
        try:
            n = int(row.get("num", ""))
        except (ValueError, TypeError):
            continue
        if max_num is None or n > max_num:
            max_num = n
    return max_num


def split_text_by_duration(text: str, num_chunks: int):
    """Делит текст на num_chunks частей ПО ГРАНИЦАМ ПРЕДЛОЖЕНИЙ (никогда
    не разрезает предложение пополам)."""
    sentences = split_sentences(text)
    if not sentences:
        return [text] if text.strip() else []

    total_words = sum(len(s.split()) for s in sentences)
    target_words_per_chunk = total_words / num_chunks

    chunks = []
    current = []
    current_words = 0
    for s in sentences:
        current.append(s)
        current_words += len(s.split())
        if current_words >= target_words_per_chunk and len(chunks) < num_chunks - 1:
            chunks.append(" ".join(current))
            current = []
            current_words = 0
    if current:
        chunks.append(" ".join(current))
    return chunks


def loose_normalize(text: str) -> str:
    """Мягкая нормализация для сравнения текста: без знаков препинания и
    регистра, чтобы не путать реальные пропуски с оформлением."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def split_sentences(text: str):
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def find_missing_sentences(original_text: str, csv_text: str, min_len: int = 15):
    """Находит предложения исходного текста, которых нет в voiceover_ru CSV."""
    try:
        reader = csv.DictReader(io.StringIO(csv_text), delimiter=";")
        voiceover_concat = " ".join(r.get("voiceover_ru", "") for r in reader)
    except Exception:
        return []

    voiceover_loose = loose_normalize(voiceover_concat)
    sentences = split_sentences(original_text)
    return [s for s in sentences if len(s) >= min_len and loose_normalize(s) not in voiceover_loose]


def _call_model_once(voiceover_text, library, master_prompt, duration_sec, start_num,
                      client, model, missing_from_previous=None):
    min_frames = -(-duration_sec // 10)
    max_frames = -(-duration_sec // 8)

    correction_note = ""
    if missing_from_previous:
        examples = "\n".join(f'- "{s}"' for s in missing_from_previous[:15])
        correction_note = (
            f"\n\nВНИМАНИЕ: в предыдущей попытке ты ПРОПУСТИЛ следующие предложения "
            f"исходного текста - в этот раз ОБЯЗАТЕЛЬНО включи их все, дословно, "
            f"в подходящие строки voiceover_ru:\n{examples}\n"
        )

    user_content = (
        f"ВСЕ НЕОБХОДИМЫЕ ДАННЫЕ УЖЕ ПРЕДОСТАВЛЕНЫ НИЖЕ. Не задавай уточняющих вопросов "
        f"про время озвучки, нумерацию кадров или библиотеку объектов — всё это уже есть "
        f"в этом сообщении.\n\n"
        f"КРИТИЧЕСКИ ВАЖНО: сгенерируй ПОЛНОСТЬЮ ВЕСЬ CSV одним сплошным ответом, "
        f"покрывающий кадрами все {duration_sec} секунд озвучки до самого конца. "
        f"При длительности кадра 8-10 секунд это ОБЯЗАТЕЛЬНО означает примерно "
        f"{min_frames}-{max_frames} строк (кадров) в итоговом CSV — НЕ МЕНЬШЕ. "
        f"Если у тебя получается заметно меньше строк — ты ошибаешься, пересчитай и "
        f"добавь недостающие кадры, пока не покроешь все {duration_sec} секунд."
        f"{correction_note}\n\n"
        f"НЕ останавливайся на середине. НЕ проси подтверждения продолжить. "
        f"НЕ добавляй никакого текста до или после CSV — ни преамбулы, ни пояснений, "
        f"ни markdown-обрамления тройными кавычками. Ответ должен состоять ТОЛЬКО из "
        f"строк CSV, начиная сразу с заголовка колонок.\n\n"
        f"ОБЩЕЕ ВРЕМЯ ОЗВУЧКИ БЛОКА: {duration_sec} секунд\n"
        f"НАЧИНАТЬ НУМЕРАЦИЮ КАДРОВ (num) С: {start_num}\n\n"
        f"БИБЛИОТЕКА ОБЪЕКТОВ (OBJECT_LIBRARY.md):\n{library}\n\n"
        f"ТЕКСТ ОЗВУЧКИ БЛОКА:\n{voiceover_text}"
    )
    response = client.chat.completions.create(
        model=model, max_completion_tokens=16384,
        messages=[
            {"role": "system", "content": master_prompt},
            {"role": "user", "content": user_content},
        ],
    )
    print(f"    finish_reason: {response.choices[0].finish_reason}, "
          f"токенов: {response.usage.total_tokens}")
    return strip_markdown_fence(response.choices[0].message.content)


def call_model(voiceover_text, library, master_prompt, duration_sec, start_num, client, model,
                max_coverage_retries: int = 2):
    """Генерирует CSV и проверяет, что весь текст дословно попал в voiceover_ru.
    Если что-то пропущено - автоматически повторяет запрос, явно указывая
    модели, какие именно предложения она пропустила в прошлый раз."""
    missing = None
    csv_text = None

    for attempt in range(max_coverage_retries + 1):
        csv_text = _call_model_once(voiceover_text, library, master_prompt, duration_sec,
                                     start_num, client, model, missing_from_previous=missing)
        missing = find_missing_sentences(voiceover_text, csv_text)
        if not missing:
            return csv_text
        print(f"    [!] Найдено пропущенных предложений: {len(missing)}"
              + (f" (попытка {attempt + 1}/{max_coverage_retries + 1})" if attempt < max_coverage_retries else ""))
        if attempt < max_coverage_retries:
            print("    Повторяю запрос с указанием на пропуски...")

    print(f"    [!!!] После {max_coverage_retries + 1} попыток всё ещё есть "
          f"{len(missing)} пропущенных предложений - сохраняю как есть, "
          f"проверь check_voiceover_coverage.py отдельно.")
    return csv_text


def shift_csv_times(csv_text: str, time_offset: int):
    """Сдвигает колонки start/end на time_offset секунд.
    Бросает ValueError, если в CSV есть повреждённые строки (лишние поля
    из-за случайной ';' внутри промта модели) - чтобы вызывающий код мог
    повторить запрос вместо тихой порчи данных."""
    reader = csv.DictReader(io.StringIO(csv_text), delimiter=";", restkey="_EXTRA_")
    fieldnames = reader.fieldnames
    rows = list(reader)
    if not rows or fieldnames is None:
        return csv_text, None

    bad_rows = [i for i, row in enumerate(rows) if row.get("_EXTRA_")]
    if bad_rows:
        raise ValueError(f"повреждённые строки (лишние поля из-за ';' внутри промта): {bad_rows[:5]}")

    for row in rows:
        try:
            row["start"] = str(int(row["start"]) + time_offset)
            row["end"] = str(int(row["end"]) + time_offset)
        except (ValueError, KeyError, TypeError):
            pass
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, delimiter=";")
    writer.writeheader()
    writer.writerows(rows)
    try:
        new_cumulative_end = int(rows[-1]["end"])
    except (ValueError, KeyError, TypeError):
        new_cumulative_end = None
    return output.getvalue(), new_cumulative_end


def generate_csv_smart(voiceover_text, library, master_prompt, duration_sec,
                        start_num, client, model):
    if duration_sec <= MAX_SECONDS_PER_REQUEST:
        return call_model(voiceover_text, library, master_prompt, duration_sec,
                           start_num, client, model)

    num_chunks = -(-duration_sec // MAX_SECONDS_PER_REQUEST)
    text_chunks = split_text_by_duration(voiceover_text, num_chunks)
    num_chunks = len(text_chunks)

    csv_parts, current_start, time_offset = [], start_num, 0
    header_seen = False
    for i, chunk_text in enumerate(text_chunks):
        remaining_chunks = num_chunks - i
        remaining_duration = duration_sec - time_offset
        chunk_dur = remaining_duration // remaining_chunks
        if i == num_chunks - 1:
            chunk_dur = remaining_duration
        print(f"    Часть {i + 1}/{num_chunks}: {chunk_dur} сек, start_num={current_start}, "
              f"время с {time_offset} сек")

        MAX_CHUNK_RETRIES = 2
        shifted_csv, new_cumulative_end = None, None
        for attempt in range(MAX_CHUNK_RETRIES + 1):
            chunk_csv = call_model(chunk_text, library, master_prompt, chunk_dur,
                                    current_start, client, model)
            try:
                shifted_csv, new_cumulative_end = shift_csv_times(chunk_csv, time_offset)
                break
            except ValueError as e:
                print(f"    [!] Часть {i + 1} повреждена ({e}).")
                if attempt < MAX_CHUNK_RETRIES:
                    print(f"    Повторяю запрос для этой части (попытка {attempt + 2}/{MAX_CHUNK_RETRIES + 1})...")
                else:
                    print("ОШИБКА: не удалось получить корректный CSV для этой части "
                          f"после {MAX_CHUNK_RETRIES + 1} попыток. Останавливаюсь.")
                    sys.exit(1)

        lines = shifted_csv.strip().split("\n")
        if not lines:
            continue
        if not header_seen:
            csv_parts.append(shifted_csv.strip())
            header_seen = True
        else:
            csv_parts.append("\n".join(lines[1:]))

        max_num = get_max_num_from_text(shifted_csv)
        if max_num is not None:
            current_start = max_num + 1

        if new_cumulative_end is not None:
            time_offset = new_cumulative_end
        else:
            time_offset += chunk_dur

    return "\n".join(csv_parts)


# ---------- основной процесс ----------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", required=True, help="Папка с CSV/MP3 файлами блоков")
    parser.add_argument("--texts-dir", required=True, help="Папка с исходными текстами блоков (.txt)")
    parser.add_argument("--library", required=True)
    parser.add_argument("--master", required=True)
    parser.add_argument("--broken", required=True, help="Имена блоков через запятую, которые нужно перегенерировать")
    parser.add_argument("--start-num", type=int, default=1, help="С какого num начинается самый первый блок")
    parser.add_argument("--manual-durations", default=None,
                         help="Длительности вручную для сломанных блоков, если нет ffprobe/mp3: 'имя=сек,имя2=сек'")
    parser.add_argument("--model", default=MODEL)
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("ОШИБКА: не задан OPENAI_API_KEY")
        sys.exit(1)
    base_url = os.environ.get("OPENAI_BASE_URL")
    client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)

    library = read_file(args.library)
    master_prompt = read_file(args.master)

    broken_set = set(x.strip() for x in args.broken.split(","))

    manual_durations = {}
    if args.manual_durations:
        for pair in args.manual_durations.split(","):
            name, sec = pair.split("=")
            manual_durations[name.strip()] = int(sec.strip())

    result_dir = Path(args.dir)
    texts_dir = Path(args.texts_dir)
    csv_files = sorted(result_dir.glob("*.csv"), key=natural_sort_key)

    if not csv_files:
        print(f"В папке {result_dir} не найдено .csv файлов")
        sys.exit(1)

    current_start_num = args.start_num
    print(f"Начинаю с num={current_start_num}\n")

    for csv_path in csv_files:
        block_name = csv_path.stem
        print(f"=== {block_name} ===")

        if block_name in broken_set:
            duration = manual_durations.get(block_name)
            if duration is None:
                mp3_path = csv_path.with_suffix(".mp3")
                if mp3_path.exists():
                    duration = get_audio_duration_ffprobe(mp3_path)
            if duration is None:
                print(f"  ОШИБКА: не удалось определить длительность для {block_name}. "
                      f"Передай через --manual-durations '{block_name}=число_секунд,...'")
                sys.exit(1)

            text_path = texts_dir / f"{block_name}.txt"
            if not text_path.exists():
                print(f"  ОШИБКА: не найден исходный текст {text_path}")
                sys.exit(1)
            voiceover_text = read_file(text_path)

            print(f"  Перегенерирую ({duration} сек, start_num={current_start_num})...")
            csv_text = generate_csv_smart(voiceover_text, library, master_prompt,
                                           duration, current_start_num, client, args.model)
            csv_path.write_text(csv_text, encoding="utf-8")
            max_num = get_max_num_from_text(csv_text)
            if max_num is None:
                print(f"  ОШИБКА: не удалось определить последний num после перегенерации {block_name}")
                sys.exit(1)
            current_start_num = max_num + 1
            print(f"  Готово, следующий блок начнётся с num={current_start_num}")

        else:
            print(f"  Перенумеровываю (без обращения к API), start_num={current_start_num}...")
            last_num, is_corrupted = renumber_csv(csv_path, current_start_num)
            if is_corrupted:
                print(f"\nОСТАНОВКА: блок {block_name} повреждён (не был в списке --broken, "
                      f"но структура CSV сломана). Добавь его в --broken и запусти заново:")
                print(f"  --broken {block_name},{args.broken}")
                sys.exit(1)
            current_start_num = last_num + 1
            print(f"  Готово, следующий блок начнётся с num={current_start_num}")

        print()

    print("=== Готово! Все блоки исправлены и сквозная нумерация восстановлена. ===")


if __name__ == "__main__":
    main()
