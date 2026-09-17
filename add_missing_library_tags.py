"""
Добавляет в OBJECT_LIBRARY.md ТОЛЬКО те теги, которых там не хватает —
вместо того чтобы дорого и долго перегенерировать всю библиотеку заново.

Находит теги, которые встречаются 2+ раза в ref_tags по всем CSV
сценария, но отсутствуют в библиотеке (та же логика, что в
check_library_coverage.py — теги, для которых уже сработал бы нечёткий
поиск при генерации, пропускаются, их трогать не нужно). Для каждого
такого тега берёт несколько примеров сцен, где он используется, просит
ИИ написать ОДНУ запись в том же формате, что и остальная библиотека, и
дописывает эти записи в конец файла (существующие записи не трогает).

ИСПОЛЬЗОВАНИЕ:
    python3 add_missing_library_tags.py --csv-dir результаты \
        --library OBJECT_LIBRARY.md --master MASTER_ПРОМТ_ДОБАВИТЬ_ТЕГ.txt \
        --model gpt-5.1
"""

import argparse
import csv
import difflib
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

try:
    from openai import OpenAI
except ImportError:
    print("Не установлена библиотека openai. Выполните: pip install openai")
    sys.exit(1)

TAG_RE = re.compile(r'^([A-Z][A-Z0-9_]{2,})\s*$')
FUZZY_CUTOFF = 0.72  # то же значение, что использует expand_tags() при реальной генерации
MAX_CONTEXT_EXAMPLES = 5  # хватит нескольких сцен, не нужны все подряд


def natural_sort_key(path):
    match = re.match(r"^(\d+)", path.stem)
    if match:
        return (0, int(match.group(1)), path.stem)
    return (-1, 0, path.stem)


def parse_library_tags(library_path: Path):
    tags = set()
    for line in library_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if TAG_RE.match(line) and "_" in line:
            tags.add(line)
    return tags


def collect_tag_contexts(csv_dir: Path):
    """Возвращает Counter(тег -> кол-во) и dict(тег -> список кусков
    контекста сцен, где он используется)."""
    counter = Counter()
    contexts = defaultdict(list)
    csv_files = sorted(csv_dir.glob("*.csv"), key=natural_sort_key)
    for csv_path in csv_files:
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, delimiter=";")
            for row in reader:
                ref_tags_field = (row.get("ref_tags") or "").strip()
                if not ref_tags_field or ref_tags_field == "-":
                    continue
                for tag in (t.strip() for t in ref_tags_field.split(",")):
                    if not tag:
                        continue
                    counter[tag] += 1
                    if len(contexts[tag]) < MAX_CONTEXT_EXAMPLES:
                        piece = " | ".join(filter(None, [
                            row.get("scene_ru", "").strip(),
                            row.get("img_prompt_1", "").strip(),
                            row.get("img_prompt_2", "").strip(),
                        ]))
                        if piece:
                            contexts[tag].append(piece)
    return counter, contexts, csv_files


def strip_wrapping(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


def generate_entry_for_tag(tag, examples, master_prompt, client, model):
    context_text = "\n".join(f"- {c}" for c in examples)
    user_content = f"Тег: {tag}\n\nПримеры сцен, где этот объект упоминается в сценарии:\n{context_text}"
    response = client.chat.completions.create(
        model=model,
        max_completion_tokens=1000,
        messages=[
            {"role": "system", "content": master_prompt},
            {"role": "user", "content": user_content},
        ],
    )
    return strip_wrapping(response.choices[0].message.content)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv-dir", required=True, help="Папка со всеми CSV сценария (например результаты)")
    parser.add_argument("--library", required=True, help="Файл OBJECT_LIBRARY.md (дописывается в конец)")
    parser.add_argument("--master", required=True, help="MASTER_ПРОМТ_ДОБАВИТЬ_ТЕГ.txt")
    parser.add_argument("--model", default="gpt-5.1")
    parser.add_argument("--min-count", type=int, default=2,
                         help="С какого количества повторов тег считается недостающим (по умолчанию 2)")
    args = parser.parse_args()

    openai_key = os.environ.get("OPENAI_API_KEY")
    openai_base_url = os.environ.get("OPENAI_BASE_URL")
    if not openai_key:
        print("ОШИБКА: не задан OPENAI_API_KEY")
        sys.exit(1)
    client = OpenAI(api_key=openai_key, base_url=openai_base_url) if openai_base_url \
        else OpenAI(api_key=openai_key)

    csv_dir = Path(args.csv_dir)
    library_path = Path(args.library)
    if not library_path.exists():
        print(f"ОШИБКА: файл библиотеки не найден: {library_path}")
        sys.exit(1)
    master_prompt = Path(args.master).read_text(encoding="utf-8")

    library_tags = parse_library_tags(library_path)
    counter, contexts, csv_files = collect_tag_contexts(csv_dir)
    if not csv_files:
        print(f"[!] В {csv_dir} не найдено CSV.")
        sys.exit(1)

    missing = []
    for tag, count in counter.most_common():
        if count < args.min_count or tag in library_tags:
            continue
        if difflib.get_close_matches(tag, library_tags, n=1, cutoff=FUZZY_CUTOFF):
            continue  # уже покрыт похожим тегом (сработает нечёткий поиск) - трогать не нужно
        missing.append(tag)

    if not missing:
        print("Недостающих тегов (с учётом порога повторов) не найдено - добавлять нечего.")
        return

    print(f"[i] Найдено недостающих тегов: {len(missing)} -> {', '.join(missing)}")

    new_entries = []
    for i, tag in enumerate(missing, 1):
        print(f"[{i}/{len(missing)}] Генерирую запись для '{tag}'...")
        entry = generate_entry_for_tag(tag, contexts[tag], master_prompt, client, args.model)
        print(f"  [+] Готово: {entry.splitlines()[0] if entry else '(пусто)'}")
        new_entries.append(entry)

    with open(library_path, "a", encoding="utf-8") as f:
        f.write("\n\n")
        f.write("\n\n".join(new_entries))
        f.write("\n")

    print(f"\nГотово! Добавлено записей: {len(new_entries)}. Файл обновлён: {library_path}")


if __name__ == "__main__":
    main()
