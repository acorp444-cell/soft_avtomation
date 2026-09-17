"""
Проверяет консистентность OBJECT_LIBRARY.md автоматически, вместо того
чтобы вручную отслеживать, сколько раз и под каким тегом что встречается
в сценарии.

Смотрит на столбец ref_tags во ВСЕХ CSV сразу (весь сценарий целиком, не
по одному блоку) и считает, сколько раз встречается каждый тег. Тег,
который встречается 1 раз - это нормально, что его нет в библиотеке (так
и задумано - библиотека нужна для консистентности ПОВТОРЯЮЩИХСЯ объектов).
А вот тег, который встречается 2+ раза и при этом отсутствует в
библиотеке (даже с учётом нечёткого поиска, который использует сама
генерация картинок) - это как раз то, что стоит добавить в библиотеку
вручную или перегенерировать библиотеку заново.

ИСПОЛЬЗОВАНИЕ:
    python3 check_library_coverage.py --csv-dir результаты --library OBJECT_LIBRARY.md
"""

import argparse
import csv
import difflib
import re
from collections import Counter
from pathlib import Path

TAG_RE = re.compile(r'^([A-Z][A-Z0-9_]{2,})\s*$')
FUZZY_CUTOFF = 0.72  # то же значение, что использует expand_tags() при реальной генерации


def natural_sort_key(path):
    match = re.match(r"^(\d+)", path.stem)
    if match:
        return (0, int(match.group(1)), path.stem)
    return (-1, 0, path.stem)


def parse_library_tags(library_path: Path):
    """Только названия тегов (заголовков) из библиотеки - для проверки
    покрытия не нужно разбирать Description/Mandatory/Never целиком."""
    tags = set()
    for line in library_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if TAG_RE.match(line):
            tags.add(line)
    return tags


def count_ref_tags(csv_dir: Path):
    counter = Counter()
    csv_files = sorted(csv_dir.glob("*.csv"), key=natural_sort_key)
    if not csv_files:
        print(f"[!] В {csv_dir} не найдено ни одного CSV.")
        return counter, []
    for csv_path in csv_files:
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, delimiter=";")
            for row in reader:
                ref_tags_field = (row.get("ref_tags") or "").strip()
                if not ref_tags_field or ref_tags_field == "-":
                    continue
                for tag in (t.strip() for t in ref_tags_field.split(",")):
                    if tag:
                        counter[tag] += 1
    return counter, csv_files


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv-dir", required=True, help="Папка со всеми CSV сценария (например результаты)")
    parser.add_argument("--library", required=True, help="Файл OBJECT_LIBRARY.md")
    parser.add_argument("--min-count", type=int, default=2,
                         help="С какого количества повторов тег считается 'должен быть в библиотеке' (по умолчанию 2)")
    args = parser.parse_args()

    csv_dir = Path(args.csv_dir)
    library_path = Path(args.library)
    if not library_path.exists():
        print(f"ОШИБКА: файл библиотеки не найден: {library_path}")
        return

    library_tags = parse_library_tags(library_path)
    print(f"[i] В библиотеке найдено тегов: {len(library_tags)}")

    counter, csv_files = count_ref_tags(csv_dir)
    if not csv_files:
        return
    print(f"[i] Проверено CSV-файлов: {len(csv_files)}")
    print(f"[i] Разных тегов в ref_tags по всему сценарию: {len(counter)}\n")

    missing = []
    fuzzy_matched = []
    for tag, count in counter.most_common():
        if count < args.min_count:
            continue
        if tag in library_tags:
            continue
        close = difflib.get_close_matches(tag, library_tags, n=1, cutoff=FUZZY_CUTOFF)
        if close:
            fuzzy_matched.append((tag, count, close[0]))
        else:
            missing.append((tag, count))

    if missing:
        print(f"=== ДЕЙСТВИТЕЛЬНО ОТСУТСТВУЮТ в библиотеке (встречаются {args.min_count}+ раз) ===")
        print("Эти теги стоит добавить в библиотеку вручную или перегенерировать библиотеку заново:\n")
        for tag, count in missing:
            print(f"  {tag} - встречается {count} раз(а)")
        print()
    else:
        print(f"=== Отсутствующих (при {args.min_count}+ повторах) тегов не найдено - хорошо! ===\n")

    if fuzzy_matched:
        print("=== Есть похожий тег в библиотеке (сработает нечёткий поиск при генерации, "
              "но лучше поправить название в CSV на точное) ===\n")
        for tag, count, closest in fuzzy_matched:
            print(f"  {tag} ({count} раз) -> похоже на '{closest}'")
        print()

    once_count = sum(1 for tag, count in counter.items() if count == 1)
    print(f"[i] Тегов, встречающихся только 1 раз (нормально, что их нет в библиотеке): {once_count}")


if __name__ == "__main__":
    main()
