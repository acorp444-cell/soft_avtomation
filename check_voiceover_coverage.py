"""
Проверяет, что колонка voiceover_ru в CSV дословно покрывает весь исходный
текст блока, без пропущенных предложений (сверяет со всеми .txt файлами
блоков и соответствующими .csv в указанных папках).

ИСПОЛЬЗОВАНИЕ:
    python3 check_voiceover_coverage.py --texts-dir "тексты блоков" --csv-dir результаты
"""

import argparse
import csv
import re
from pathlib import Path



def natural_sort_key(path):
    """Сортирует файлы по числу в начале имени (1, 2, ..., 10, 11), а не
    по алфавиту как текст (где "10" встаёт перед "2"). Файлы без числа
    в начале (например "хук") идут ПЕРВЫМИ, как вступление перед блоком 1."""
    match = re.match(r"^(\d+)", path.stem)
    if match:
        return (0, int(match.group(1)), path.stem)
    return (-1, 0, path.stem)  # "хук" и подобные - идут ПЕРВЫМИ, как вступление


def normalize(text: str) -> str:
    """Убирает лишние пробелы/переносы строк для более честного сравнения
    (используется для расчёта процента покрытия по объёму текста)."""
    return re.sub(r"\s+", " ", text).strip()


def loose_normalize(text: str) -> str:
    """Более мягкая нормализация для СРАВНЕНИЯ предложений: убирает знаки
    препинания и приводит к нижнему регистру, чтобы не считать
    'пропуском' мелкие расхождения в оформлении (кавычки, тире, заглавные
    буквы, точки/запятые), если смысл и слова на месте."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def split_sentences(text: str):
    """Простое разбиение на предложения по точке/!/?, с фильтрацией пустых."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def get_voiceover_concat(csv_path: Path) -> str:
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        rows = list(reader)
    return normalize(" ".join(r.get("voiceover_ru", "") for r in rows))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--texts-dir", required=True, help="Папка с исходными текстами блоков (.txt)")
    parser.add_argument("--csv-dir", required=True, help="Папка с готовыми CSV блоков")
    parser.add_argument("--min-missing-len", type=int, default=15,
                         help="Игнорировать 'пропущенные' совпадения короче N символов (мелкие расхождения)")
    args = parser.parse_args()

    texts_dir = Path(args.texts_dir)
    csv_dir = Path(args.csv_dir)

    txt_files = sorted(texts_dir.glob("*.txt"), key=natural_sort_key)
    if not txt_files:
        print(f"В папке {texts_dir} не найдено .txt файлов")
        return

    print(f"{'Блок':<20} {'Покрытие текста':<18} {'Пропущено предложений':<22} Статус")
    print("-" * 80)

    problems = []

    for txt_path in txt_files:
        block_name = txt_path.stem
        csv_path = csv_dir / f"{block_name}.csv"

        if not csv_path.exists():
            print(f"{block_name:<20} {'?':<18} {'?':<22} нет CSV")
            continue

        original_text = normalize(txt_path.read_text(encoding="utf-8"))
        voiceover_text = get_voiceover_concat(csv_path)

        coverage_percent = round(len(voiceover_text) / len(original_text) * 100, 1) if original_text else 0

        sentences = split_sentences(original_text)
        voiceover_loose = loose_normalize(voiceover_text)
        missing = [s for s in sentences
                   if len(s) >= args.min_missing_len and loose_normalize(s) not in voiceover_loose]

        if coverage_percent < 90 or missing:
            status = f"ПРОПУСКИ - ПЕРЕГЕНЕРИРОВАТЬ"
            problems.append(block_name)
        else:
            status = "OK"

        print(f"{block_name:<20} {str(coverage_percent) + '%':<18} {len(missing):<22} {status}")

        if missing:
            for s in missing[:3]:  # показываем первые 3 примера, чтобы не заваливать вывод
                preview = s[:80] + ("..." if len(s) > 80 else "")
                print(f"      пропущено: \"{preview}\"")
            if len(missing) > 3:
                print(f"      ...и ещё {len(missing) - 3} предложений")

    print("\n" + "=" * 80)
    if problems:
        print(f"Нужно перегенерировать ({len(problems)}): {', '.join(problems)}")
    else:
        print("Все проверенные блоки дословно покрывают исходный текст!")


if __name__ == "__main__":
    main()
