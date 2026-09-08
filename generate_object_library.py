"""
Генерирует OBJECT_LIBRARY.md по ПОЛНОМУ ТЕКСТУ СЦЕНАРИЯ (все блоки сразу),
чтобы объекты были едиными для всего фильма ещё до генерации CSV по блокам.

ИСПОЛЬЗОВАНИЕ:
    python generate_object_library.py --blocks-dir "тексты блоков" --master MASTER_ПРОМТ_OBJECT_LIBRARY.txt --output OBJECT_LIBRARY.md

Перед запуском (в терминале, один раз в сессию):
    export OPENAI_API_KEY="..."
    export OPENAI_BASE_URL="..."   (если используется сервис-агрегатор)

ТРЕБОВАНИЯ:
    pip install openai
"""

import argparse
import re
import os
import sys
from pathlib import Path

try:
    from openai import OpenAI
except ImportError:
    print("Не установлена библиотека openai. Выполните: pip install openai")
    sys.exit(1)

MODEL = "gpt-4o"
MAX_CHARS_PER_CHUNK = 30000  # безопасный объём текста сценария на один запрос



def natural_sort_key(path):
    """Сортирует файлы по числу в начале имени (1, 2, ..., 10, 11), а не
    по алфавиту как текст (где "10" встаёт перед "2"). Файлы без числа
    в начале (например "хук") идут ПЕРВЫМИ, как вступление перед блоком 1."""
    match = re.match(r"^(\d+)", path.stem)
    if match:
        return (0, int(match.group(1)), path.stem)
    return (-1, 0, path.stem)  # "хук" и подобные - идут ПЕРВЫМИ, как вступление


def read_file(path: str) -> str:
    p = Path(path)
    if not p.exists():
        print(f"ОШИБКА: файл не найден: {path}")
        sys.exit(1)
    return p.read_text(encoding="utf-8")


def strip_wrapping(text: str) -> str:
    """Убирает возможное markdown-обрамление и преамбулу, если модель их всё же добавила."""
    text = text.strip()
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            candidate = part.strip()
            first_line, _, rest = candidate.partition("\n")
            if first_line.strip().lower() in ("markdown", "text", "plaintext", ""):
                candidate = rest.strip()
            if "Description:" in candidate:
                return candidate
    return text


def split_blocks_into_chunks(block_files, max_chars):
    """Группирует файлы блоков в куски так, чтобы суммарный объём текста
    в каждом куске не превышал max_chars (границы блоков не разрезаются)."""
    chunks = []
    current_files = []
    current_len = 0

    for f in block_files:
        text_len = len(read_file(str(f)))
        if current_files and current_len + text_len > max_chars:
            chunks.append(current_files)
            current_files = []
            current_len = 0
        current_files.append(f)
        current_len += text_len

    if current_files:
        chunks.append(current_files)

    return chunks


def call_model_for_library(script_text, master_prompt, client, model, max_tokens=6000):
    response = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": master_prompt},
            {"role": "user", "content": f"ТЕКСТ СЦЕНАРИЯ (может быть частью целого):\n\n{script_text}"},
        ],
    )
    finish_reason = response.choices[0].finish_reason
    result_text = response.choices[0].message.content
    print(f"  finish_reason: {finish_reason}, токенов: {response.usage.total_tokens}")
    return strip_wrapping(result_text)


MERGE_INSTRUCTION = """
Ниже — несколько черновиков библиотеки объектов (OBJECT_LIBRARY), созданных
по разным частям одного и того же сценария. Объедини их в ОДНУ единую
библиотеку:

- если один и тот же объект встречается в нескольких черновиках под разными
  тегами - выбери один тег и объедини описания в одно
- убери дублирующиеся объекты
- сохрани все разделы и структуру формата (Description/Mandatory features/
  Never, разделы заглавными буквами, раздел VISUAL CONCEPTS в конце)
- ничего не добавляй от себя, только объединяй и дедуплицируй то, что есть
  в черновиках

Выведи результат СТРОГО в том же формате, что и черновики - без преамбулы,
без пояснений, без markdown-обрамления.
"""


def merge_libraries(partial_libraries, master_prompt, client, model):
    print(f"Объединяю {len(partial_libraries)} черновиков библиотеки в один...")
    combined_drafts = "\n\n=== СЛЕДУЮЩИЙ ЧЕРНОВИК ===\n\n".join(partial_libraries)
    response = client.chat.completions.create(
        model=model,
        max_tokens=8000,
        messages=[
            {"role": "system", "content": master_prompt},
            {"role": "user", "content": MERGE_INSTRUCTION + "\n\n" + combined_drafts},
        ],
    )
    finish_reason = response.choices[0].finish_reason
    result_text = response.choices[0].message.content
    print(f"  finish_reason: {finish_reason}, токенов: {response.usage.total_tokens}")
    return strip_wrapping(result_text)


def main():
    parser = argparse.ArgumentParser(description="Генерация OBJECT_LIBRARY.md по всему сценарию")
    parser.add_argument("--blocks-dir", required=True, help="Папка с текстовыми файлами всех блоков сценария (.txt)")
    parser.add_argument("--master", required=True, help="Файл мастер-промта для генерации библиотеки")
    parser.add_argument("--output", required=True, help="Куда сохранить OBJECT_LIBRARY.md")
    parser.add_argument("--model", default=MODEL, help=f"Модель (по умолчанию {MODEL})")
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("ОШИБКА: не задан OPENAI_API_KEY")
        sys.exit(1)
    base_url = os.environ.get("OPENAI_BASE_URL")

    master_prompt = read_file(args.master)

    blocks_dir = Path(args.blocks_dir)
    block_files = sorted(blocks_dir.glob("*.txt"), key=natural_sort_key)
    if not block_files:
        print(f"ОШИБКА: в папке {blocks_dir} не найдено .txt файлов")
        sys.exit(1)

    print(f"Найдено блоков сценария: {len(block_files)}")
    total_chars = sum(len(read_file(str(f))) for f in block_files)
    print(f"Общий объём сценария: {total_chars} символов")

    client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)

    chunks = split_blocks_into_chunks(block_files, MAX_CHARS_PER_CHUNK)

    if len(chunks) == 1:
        print(f"Отправляю запрос (модель {args.model})...")
        full_script = "\n\n".join(
            f"=== {f.stem} ===\n{read_file(str(f))}" for f in chunks[0]
        )
        result_text = call_model_for_library(full_script, master_prompt, client, args.model)
    else:
        print(f"Сценарий большой - делю на {len(chunks)} частей, чтобы уложиться в лимит токенов OpenAI")
        partial_libraries = []
        for i, chunk_files in enumerate(chunks, 1):
            chunk_text = "\n\n".join(
                f"=== {f.stem} ===\n{read_file(str(f))}" for f in chunk_files
            )
            names = ", ".join(f.stem for f in chunk_files)
            print(f"\nЧасть {i}/{len(chunks)} ({names})...")
            partial = call_model_for_library(chunk_text, master_prompt, client, args.model)
            partial_libraries.append(partial)

        result_text = merge_libraries(partial_libraries, master_prompt, client, args.model)

    output_path = Path(args.output)
    output_path.write_text(result_text, encoding="utf-8")

    print(f"\nГотово! OBJECT_LIBRARY сохранён: {output_path}")


if __name__ == "__main__":
    main()
