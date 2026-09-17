"""
Генерирует промт(ы) для фоновой ИНСТРУМЕНТАЛЬНОЙ музыки под весь фильм
(для Suno или похожего ИИ-генератора музыки) - на основе анализа всего
сценария целиком. Музыка одна на весь фильм, не под отдельные кадры.

ИСПОЛЬЗОВАНИЕ:
    python3 generate_music_prompt.py --blocks-dir "тексты блоков" \
        --master MASTER_ПРОМТ_MUSIC.txt --output-dir превью

Перед запуском:
    export OPENAI_API_KEY="..."
    export OPENAI_BASE_URL="..."   (если нужен сервис-агрегатор)

ТРЕБОВАНИЯ:
    pip install openai
"""

import argparse
import os
import re
import sys
from pathlib import Path

try:
    from openai import OpenAI
except ImportError:
    print("Не установлена библиотека openai. Выполните: pip install openai")
    sys.exit(1)

TEXT_MODEL = "gpt-4o"


def natural_sort_key(path):
    """Та же логика, что в остальных скриптах - файлы по числу в начале
    имени (1, 2, ..., 10, 11), файлы без числа (например "хук") - первыми."""
    match = re.match(r"^(\d+)", path.stem)
    if match:
        return (0, int(match.group(1)), path.stem)
    return (-1, 0, path.stem)


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


MAX_CHARS_FOR_MUSIC = 30000  # чтобы не упереться в лимит токенов в минуту (TPM) на длинных сценариях


def truncate_for_prompt(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    print(f"  [i] Сценарий длинный ({len(text)} символов) - беру только первые "
          f"{max_chars} (экономия токенов/лимита OpenAI). Для музыки этого достаточно - "
          f"настроение и тема фильма обычно понятны уже по первой части сценария.")
    return text[:max_chars] + "\n\n[...остальной текст сценария сокращён для экономии токенов...]"


def generate_music_prompts(full_script: str, master_prompt: str, client, model) -> str:
    print("Анализирую сценарий и подбираю варианты фоновой музыки...")
    script_for_prompt = truncate_for_prompt(full_script, MAX_CHARS_FOR_MUSIC)
    response = client.chat.completions.create(
        model=model,
        max_completion_tokens=1500,
        messages=[
            {"role": "system", "content": master_prompt},
            {"role": "user", "content": f"ПОЛНЫЙ ТЕКСТ СЦЕНАРИЯ:\n\n{script_for_prompt}"},
        ],
    )
    print(f"токенов: {response.usage.total_tokens}")
    return strip_wrapping(response.choices[0].message.content)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--blocks-dir", required=True, help="Папка с текстами всех блоков сценария (.txt)")
    parser.add_argument("--master", required=True, help="Файл MASTER_ПРОМТ_MUSIC.txt")
    parser.add_argument("--output-dir", required=True, help="Куда сохранить music_prompt.txt")
    parser.add_argument("--text-model", default=TEXT_MODEL)
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("ОШИБКА: не задан OPENAI_API_KEY")
        sys.exit(1)
    base_url = os.environ.get("OPENAI_BASE_URL")
    client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)

    master_prompt = read_file(args.master)

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

    result = generate_music_prompts(full_script, master_prompt, client, args.text_model)

    music_path = output_dir / "music_prompt.txt"
    music_path.write_text(result, encoding="utf-8")
    print(f"\nГотово! Сохранено: {music_path}\n")
    print(result)


if __name__ == "__main__":
    main()
