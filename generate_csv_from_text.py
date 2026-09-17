"""
Скрипт для автоматической генерации CSV-файла блока сценария
из текста озвучки, библиотеки объектов и мастер-промта через OpenAI API.

Заменяет ручной процесс: загрузка текста в чат-бота -> ожидание ответа -> копирование CSV.

Если блок длинный (duration > ~220 сек), скрипт САМ автоматически делит
его на несколько запросов поменьше, чтобы не упереться в лимит длины
ответа модели, и склеивает результат в один CSV с продолжающейся
нумерацией кадров. Это происходит прозрачно, вручную ничего делать не нужно.

ИСПОЛЬЗОВАНИЕ:
    python generate_csv_from_text.py --text 5_block.txt --library OBJECT_LIBRARY.md --master master_prompt.txt --output 5_block.csv --duration 754 --start-num 301

Перед запуском один раз в терминале (Windows, PowerShell или cmd):
    set OPENAI_API_KEY=sk-...ваш ключ...
(вводить заново в каждом новом окне терминала, как и с RunPod-ключом)

Если используется сервис-агрегатор (NanoGPT, AI/ML API и т.п.) вместо
напрямую OpenAI - дополнительно задать адрес сервиса:
    set OPENAI_BASE_URL=https://адрес-сервиса/v1

ТРЕБОВАНИЯ:
    pip install openai
"""

import argparse
import csv
import io
import os
import re
import sys
from pathlib import Path

try:
    from openai import OpenAI
except ImportError:
    print("Не установлена библиотека openai. Выполните: pip install openai")
    sys.exit(1)

MODEL = "gpt-4o"
MAX_SECONDS_PER_REQUEST = 220  # безопасный потолок на один запрос к модели


def read_file(path: str) -> str:
    p = Path(path)
    if not p.exists():
        print(f"ОШИБКА: файл не найден: {path}")
        sys.exit(1)
    return p.read_text(encoding="utf-8")


def strip_markdown_fence(text: str) -> str:
    """Извлекает CSV из ответа модели, даже если он обёрнут в текст или ```."""
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


def get_max_num(csv_text: str):
    """Находит максимальное значение колонки num в тексте CSV (разделитель ';')."""
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


def split_sentences(text: str):
    """Разбивает текст на предложения по точке/!/?, с фильтрацией пустых."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def split_text_by_duration(text: str, num_chunks: int):
    """Делит текст на num_chunks частей ПО ГРАНИЦАМ ПРЕДЛОЖЕНИЙ (никогда
    не разрезает предложение пополам - иначе модель получает оборванную
    фразу без точки и не может её процитировать дословно)."""
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


def call_model(voiceover_text, library, master_prompt, duration_sec, start_num,
                client, model):
    min_frames = -(-duration_sec // 10)
    max_frames = -(-duration_sec // 8)

    user_content = (
        f"ВСЕ НЕОБХОДИМЫЕ ДАННЫЕ УЖЕ ПРЕДОСТАВЛЕНЫ НИЖЕ. Не задавай уточняющих вопросов "
        f"про время озвучки, нумерацию кадров или библиотеку объектов — всё это уже есть "
        f"в этом сообщении.\n\n"
        f"КРИТИЧЕСКИ ВАЖНО: сгенерируй ПОЛНОСТЬЮ ВЕСЬ CSV одним сплошным ответом, "
        f"покрывающий кадрами все {duration_sec} секунд озвучки до самого конца. "
        f"При длительности кадра 8-10 секунд это ОБЯЗАТЕЛЬНО означает примерно "
        f"{min_frames}-{max_frames} строк (кадров) в итоговом CSV — НЕ МЕНЬШЕ. "
        f"Если у тебя получается заметно меньше строк — ты ошибаешься, пересчитай и "
        f"добавь недостающие кадры, пока не покроешь все {duration_sec} секунд.\n\n"
        f"НЕ останавливайся на середине. НЕ проси подтверждения продолжить. "
        f"НЕ пиши фразы вроде 'вот первые строки' или 'дай знать, если нужно продолжение'. "
        f"НЕ добавляй никакого текста до или после CSV — ни преамбулы, ни пояснений, "
        f"ни markdown-обрамления тройными кавычками. Ответ должен состоять ТОЛЬКО из "
        f"строк CSV, начиная сразу с заголовка колонок.\n\n"
        f"ОБЩЕЕ ВРЕМЯ ОЗВУЧКИ БЛОКА: {duration_sec} секунд\n"
        f"НАЧИНАТЬ НУМЕРАЦИЮ КАДРОВ (num) С: {start_num}\n\n"
        f"БИБЛИОТЕКА ОБЪЕКТОВ (OBJECT_LIBRARY.md):\n{library}\n\n"
        f"ТЕКСТ ОЗВУЧКИ БЛОКА:\n{voiceover_text}"
    )

    response = client.chat.completions.create(
        model=model,
        max_completion_tokens=16384,
        messages=[
            {"role": "system", "content": master_prompt},
            {"role": "user", "content": user_content},
        ],
    )

    finish_reason = response.choices[0].finish_reason
    result_text = response.choices[0].message.content
    print(f"  Причина завершения ответа (finish_reason): {finish_reason}, "
          f"длина: {len(result_text)} символов, токенов: {response.usage.total_tokens}")

    return strip_markdown_fence(result_text)


def shift_csv_times(csv_text: str, time_offset: int):
    """Сдвигает колонки start/end на time_offset секунд. Бросает ValueError,
    если в CSV есть повреждённые строки (лишние поля из-за случайной ';'
    внутри промта модели)."""
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


def validate_csv_not_corrupted(csv_text: str):
    """Проверяет, нет ли в CSV строк с лишними полями (случайная ';' внутри
    промта модели). Бросает ValueError, если найдены."""
    reader = csv.DictReader(io.StringIO(csv_text), delimiter=";", restkey="_EXTRA_")
    rows = list(reader)
    bad_rows = [i for i, row in enumerate(rows) if row.get("_EXTRA_")]
    if bad_rows:
        raise ValueError(f"повреждённые строки (лишние поля из-за ';' внутри промта): {bad_rows[:5]}")


def generate_csv_smart(voiceover_text, library, master_prompt, duration_sec,
                        start_num, client, model):
    """Если блок длинный - делит его на несколько запросов и склеивает результат
    с продолжающейся нумерацией и продолжающимся таймлайном (start/end)."""
    if duration_sec <= MAX_SECONDS_PER_REQUEST:
        MAX_RETRIES = 2
        for attempt in range(MAX_RETRIES + 1):
            csv_text = call_model(voiceover_text, library, master_prompt,
                                   duration_sec, start_num, client, model)
            try:
                validate_csv_not_corrupted(csv_text)
                return csv_text
            except ValueError as e:
                print(f"  [!] Блок повреждён ({e}).")
                if attempt < MAX_RETRIES:
                    print(f"  Повторяю запрос (попытка {attempt + 2}/{MAX_RETRIES + 1})...")
                else:
                    print(f"ОШИБКА: не удалось получить корректный CSV после {MAX_RETRIES + 1} попыток.")
                    sys.exit(1)

    num_chunks = -(-duration_sec // MAX_SECONDS_PER_REQUEST)
    print(f"Блок длинный ({duration_sec} сек) - делю на {num_chunks} частей "
          f"по ~{duration_sec // num_chunks} сек, чтобы не упереться в лимит ответа модели")

    text_chunks = split_text_by_duration(voiceover_text, num_chunks)
    num_chunks = len(text_chunks)

    csv_parts = []
    current_start_num = start_num
    time_offset = 0
    header_line = None

    for i, chunk_text in enumerate(text_chunks):
        remaining_chunks = num_chunks - i
        remaining_duration = duration_sec - time_offset
        chunk_duration = remaining_duration // remaining_chunks
        if i == num_chunks - 1:
            chunk_duration = remaining_duration
        print(f"\nЧасть {i + 1}/{num_chunks}: {chunk_duration} сек, "
              f"начиная с num={current_start_num}, время с {time_offset} сек...")

        MAX_CHUNK_RETRIES = 2
        shifted_csv, new_cumulative_end = None, None
        for attempt in range(MAX_CHUNK_RETRIES + 1):
            chunk_csv = call_model(chunk_text, library, master_prompt,
                                    chunk_duration, current_start_num, client, model)
            try:
                shifted_csv, new_cumulative_end = shift_csv_times(chunk_csv, time_offset)
                break
            except ValueError as e:
                print(f"  [!] Часть {i + 1} повреждена ({e}).")
                if attempt < MAX_CHUNK_RETRIES:
                    print(f"  Повторяю запрос для этой части (попытка {attempt + 2}/{MAX_CHUNK_RETRIES + 1})...")
                else:
                    print("ОШИБКА: не удалось получить корректный CSV для этой части "
                          f"после {MAX_CHUNK_RETRIES + 1} попыток. Останавливаюсь.")
                    sys.exit(1)

        lines = shifted_csv.strip().split("\n")
        if not lines:
            continue

        if header_line is None:
            header_line = lines[0]
            csv_parts.append(shifted_csv.strip())
        else:
            csv_parts.append("\n".join(lines[1:]))

        max_num = get_max_num(shifted_csv)
        if max_num is not None:
            current_start_num = max_num + 1
        else:
            print(f"  [!] Не удалось определить последний num в части {i + 1}, "
                  f"нумерация может сбиться")

        if new_cumulative_end is not None:
            time_offset = new_cumulative_end
        else:
            time_offset += chunk_duration
            print(f"  [!] Не удалось определить last end в части {i + 1}, "
                  f"использую расчётное время (может немного разойтись)")

    return "\n".join(csv_parts)


def main():
    parser = argparse.ArgumentParser(description="Генерация CSV блока сценария через OpenAI API")
    parser.add_argument("--text", required=True, help="Файл с текстом озвучки блока")
    parser.add_argument("--library", required=True, help="Файл OBJECT_LIBRARY.md")
    parser.add_argument("--master", required=True, help="Файл с мастер-промтом")
    parser.add_argument("--output", required=True, help="Куда сохранить готовый CSV")
    parser.add_argument("--duration", required=True, type=int, help="Общее время озвучки блока в секундах")
    parser.add_argument("--start-num", required=True, type=int, help="С какого номера кадра (num) начинать нумерацию")
    parser.add_argument("--model", default=MODEL, help=f"Модель OpenAI (по умолчанию {MODEL})")
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("ОШИБКА: не задан OPENAI_API_KEY.")
        print('Выполните в терминале: set OPENAI_API_KEY=ваш_ключ')
        sys.exit(1)

    master_prompt = read_file(args.master)
    library = read_file(args.library)
    voiceover_text = read_file(args.text)

    base_url = os.environ.get("OPENAI_BASE_URL")
    print(f"Модель: {args.model}{', сервис: ' + base_url if base_url else ''}")
    client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)

    result_text = generate_csv_smart(voiceover_text, library, master_prompt,
                                      args.duration, args.start_num, client, args.model)

    output_path = Path(args.output)
    output_path.write_text(result_text, encoding="utf-8")

    print(f"\nГотово! CSV сохранён: {output_path}")


if __name__ == "__main__":
    main()
