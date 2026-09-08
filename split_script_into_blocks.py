"""
Автоматически разбивает ПОЛНЫЙ ТЕКСТ СЦЕНАРИЯ (один файл) на отдельные
файлы блоков - "нулевая ступень" автоматизации, заменяет ручное
копирование кусков в блокноте.

Распознаёт заголовки блоков по строкам вида:
    ХУК
    БЛОК 1. «Название блока»
    БЛОК 11. «Название»

Всё, что идёт ДО первого такого заголовка (общий заголовок сценария,
хронометраж, количество блоков и т.п.) - пропускается, в блоки не идёт.

Текст между заголовками сохраняется в отдельные файлы:
    ХУК          -> хук.txt
    БЛОК 1. ...  -> 1_блок.txt
    БЛОК 11. ... -> 11_блок.txt

ИСПОЛЬЗОВАНИЕ:
    python3 split_script_into_blocks.py --input сценарий.md --output-dir "тексты блоков"
"""

import argparse
import re
from pathlib import Path

HOOK_PATTERN = re.compile(r"^ХУК\s*$", re.IGNORECASE)
BLOCK_PATTERN = re.compile(r"^БЛОК\s+(\d+)\.?", re.IGNORECASE)


def split_script(full_text: str):
    """Возвращает список (имя_файла, текст_блока) в порядке появления."""
    lines = full_text.splitlines()

    # находим позиции всех заголовков блоков
    headers = []  # (line_index, filename)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if HOOK_PATTERN.match(stripped):
            headers.append((i, "хук.txt"))
            continue
        m = BLOCK_PATTERN.match(stripped)
        if m:
            num = m.group(1)
            headers.append((i, f"{num}_блок.txt"))

    if not headers:
        return []

    blocks = []
    for idx, (line_i, filename) in enumerate(headers):
        start = line_i + 1
        end = headers[idx + 1][0] if idx + 1 < len(headers) else len(lines)
        body_lines = lines[start:end]

        # убираем лишние пустые строки в начале/конце, схлопываем 3+ пустых
        # строки подряд до одной пустой строки между абзацами
        text = "\n".join(body_lines).strip()
        text = re.sub(r"\n{3,}", "\n\n", text)

        blocks.append((filename, text))

    return blocks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Файл с полным текстом сценария (.txt/.md)")
    parser.add_argument("--output-dir", required=True, help="Куда сохранить файлы блоков")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ОШИБКА: файл не найден: {input_path}")
        return

    full_text = input_path.read_text(encoding="utf-8")
    blocks = split_script(full_text)

    if not blocks:
        print("ОШИБКА: не найдено ни одного заголовка блока (ХУК или БЛОК N.). "
              "Проверь формат заголовков в файле сценария.")
        return

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Найдено блоков: {len(blocks)}\n")
    for filename, text in blocks:
        out_path = output_dir / filename
        out_path.write_text(text, encoding="utf-8")
        words = len(text.split())
        print(f"  {filename:<20} {words:5d} слов, {len(text):6d} символов")

    print(f"\nГотово! Файлы сохранены в: {output_dir}")


if __name__ == "__main__":
    main()
