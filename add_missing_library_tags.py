"""
Автоматически разбирается с недостающими тегами библиотеки — без
ручного анализа со стороны пользователя.

Находит теги, которые встречаются 2+ раза в ref_tags по всем CSV
сценария, но отсутствуют в библиотеке (та же логика, что в
check_library_coverage.py). Для каждого такого тега спрашивает ИИ
(с контекстом сцен и полным списком уже существующих тегов), к какому
из трёх случаев он относится:

  1. EXISTING_MATCH — это на самом деле ТОТ ЖЕ объект, что уже есть в
     библиотеке, просто написан расплывчато/сокращённо/с опечаткой
     (например "RIVER" вместо "PRIPYAT_RIVER") - в этом случае скрипт
     САМ исправляет этот тег во ВСЕХ CSV на точное существующее
     название, библиотеку не трогает.
  2. NOT_AN_OBJECT — это вообще не конкретный визуальный объект (голый
     год, абстрактная тема) - пропускается, ничего не меняется.
  3. Новая запись — это действительно новый объект - дописывается в
     конец OBJECT_LIBRARY.md (существующие записи не трогает).

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
CSV_DELIMITER = ";"


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
            reader = csv.DictReader(f, delimiter=CSV_DELIMITER)
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


def classify_tag(tag, examples, all_library_tags, master_prompt, client, model):
    """Возвращает ('existing_match', точный_тег), ('not_an_object', None)
    или ('new_entry', полный_текст_записи)."""
    context_text = "\n".join(f"- {c}" for c in examples)
    tags_list_text = ", ".join(sorted(all_library_tags))
    user_content = (
        f"Тег: {tag}\n\n"
        f"Примеры сцен, где этот тег используется:\n{context_text}\n\n"
        f"Список уже существующих тегов в библиотеке:\n{tags_list_text}"
    )
    response = client.chat.completions.create(
        model=model,
        max_completion_tokens=1000,
        messages=[
            {"role": "system", "content": master_prompt},
            {"role": "user", "content": user_content},
        ],
    )
    raw = strip_wrapping(response.choices[0].message.content)

    if raw.upper().startswith("EXISTING_MATCH"):
        _, _, target = raw.partition(":")
        target = target.strip()
        if target in all_library_tags:
            return "existing_match", target
        # модель могла чуть исказить название - подстрахуемся нечётким поиском
        close = difflib.get_close_matches(target, all_library_tags, n=1, cutoff=FUZZY_CUTOFF)
        if close:
            return "existing_match", close[0]
        # не смогли сопоставить ни с чем реальным - считаем, что объект новый
        return "new_entry", None

    if raw.upper().startswith("NOT_AN_OBJECT"):
        return "not_an_object", None

    return "new_entry", raw


def rename_tag_in_csv_files(csv_files, old_tag, new_tag, log=print):
    """Заменяет РОВНО этот тег (как отдельный элемент списка ref_tags,
    не как подстроку!) на новый во всех CSV, где он встречается. Не
    трогает файлы, где тега вообще нет."""
    total_rows_changed = 0
    for csv_path in csv_files:
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, delimiter=CSV_DELIMITER)
            fieldnames = reader.fieldnames
            rows = list(reader)

        changed = False
        for row in rows:
            ref_tags_field = (row.get("ref_tags") or "").strip()
            if not ref_tags_field or ref_tags_field == "-":
                continue
            parts = [t.strip() for t in ref_tags_field.split(",")]
            if old_tag not in parts:
                continue
            new_parts = [new_tag if p == old_tag else p for p in parts]
            # убираем возможные дубли, если новый тег там уже и так был
            seen = set()
            deduped = []
            for p in new_parts:
                if p not in seen:
                    seen.add(p)
                    deduped.append(p)
            row["ref_tags"] = ", ".join(deduped)
            changed = True
            total_rows_changed += 1

        if changed:
            with open(csv_path, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=CSV_DELIMITER)
                writer.writeheader()
                writer.writerows(rows)
            log(f"  [i] {csv_path.name}: исправлено строк с '{old_tag}' -> '{new_tag}'")

    return total_rows_changed


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
        print("Недостающих тегов (с учётом порога повторов) не найдено - делать нечего.")
        return

    print(f"[i] Найдено недостающих тегов: {len(missing)} -> {', '.join(missing)}\n")

    new_entries = []
    renamed = []
    skipped_not_object = []

    for i, tag in enumerate(missing, 1):
        print(f"[{i}/{len(missing)}] Разбираюсь с тегом '{tag}'...")
        kind, payload = classify_tag(tag, contexts[tag], library_tags, master_prompt, client, args.model)

        if kind == "existing_match":
            print(f"  [i] '{tag}' - это тот же объект, что уже есть в библиотеке под именем '{payload}'. "
                  f"Исправляю CSV...")
            rename_tag_in_csv_files(csv_files, tag, payload, log=print)
            renamed.append((tag, payload))
        elif kind == "not_an_object":
            print(f"  [i] '{tag}' - не конкретный визуальный объект, пропускаю.")
            skipped_not_object.append(tag)
        else:
            print(f"  [+] '{tag}' - новый объект, добавляю запись в библиотеку.")
            new_entries.append(payload)

    if new_entries:
        with open(library_path, "a", encoding="utf-8") as f:
            f.write("\n\n")
            f.write("\n\n".join(new_entries))
            f.write("\n")

    print("\n=== ИТОГ ===")
    print(f"Добавлено новых записей в библиотеку: {len(new_entries)}")
    if renamed:
        print(f"Исправлено в CSV (расплывчатый тег -> точный существующий): {len(renamed)}")
        for old_tag, new_tag in renamed:
            print(f"  {old_tag} -> {new_tag}")
    if skipped_not_object:
        print(f"Пропущено (не объект): {len(skipped_not_object)} -> {', '.join(skipped_not_object)}")


if __name__ == "__main__":
    main()
