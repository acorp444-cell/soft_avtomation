"""
Генерирует материалы для публикации фильма на YouTube: заголовок,
текст на превью, описание (с главами и таймингом), источники, теги -
на основе полного текста сценария (все блоки по порядку) и реальной
длительности озвучки каждого блока (измеряется через ffprobe, а не на
глаз).

Тайминг глав: модель указывает, В КАКОМ БЛОКЕ начинается глава и на
какой ДОЛЕ этого блока (0.0-1.0) - а точное время в секундах считает
сам скрипт, по реальной длительности озвучки и порядку блоков (блоки
должны идти в готовом видео строго по порядку файлов, без перестановок
и ручной обрезки - иначе тайминг придётся поправить вручную).

Работает только на RunPod (OpenAI заблокирован по региону с домашнего
интернета пользователя).

ИСПОЛЬЗОВАНИЕ:
    python3 generate_youtube_metadata.py \
        --blocks-dir блоки --audio-dir результаты \
        --master MASTER_ПРОМТ_YOUTUBE_ОПИСАНИЕ.txt \
        --output результаты/youtube_описание.txt \
        --model gpt-5.1 [--previous-films "Чернобыль, Маяк"]
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from frame_segmenter import natural_sort_key_from_stem

try:
    from openai import OpenAI
except ImportError:
    print("Не установлена библиотека openai. Выполните: pip install openai")
    sys.exit(1)

sys.stdout.reconfigure(line_buffering=True)


def get_duration_sec(audio_path: Path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def format_timestamp(total_sec: float) -> str:
    total_sec = max(0, round(total_sec))
    h, rem = divmod(total_sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def strip_wrapping(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


def parse_sections(raw: str) -> dict:
    sections = {}
    current = None
    buf = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("===") and stripped.endswith("===") and len(stripped) > 6:
            if current:
                sections[current] = "\n".join(buf).strip()
            current = stripped.strip("=").strip()
            buf = []
            continue
        if current:
            buf.append(line)
    if current:
        sections[current] = "\n".join(buf).strip()
    return sections


def build_chapters_text(chapters_raw: str, block_starts: dict, block_durations: dict, log=print):
    lines = []
    for raw_line in chapters_raw.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        parts = raw_line.split(";", 2)
        if len(parts) != 3:
            log(f"[!] Не понял строку главы (пропускаю): {raw_line}")
            continue
        block_name, fraction_str, title = (p.strip() for p in parts)
        if block_name not in block_starts:
            log(f"[!] Глава ссылается на неизвестный блок '{block_name}' (пропускаю): {raw_line}")
            continue
        try:
            fraction = max(0.0, min(1.0, float(fraction_str)))
        except ValueError:
            log(f"[!] Не понял долю '{fraction_str}' (пропускаю): {raw_line}")
            continue
        start_sec = block_starts[block_name] + fraction * block_durations[block_name]
        lines.append((start_sec, f"{format_timestamp(start_sec)} — {title}"))

    lines.sort(key=lambda x: x[0])
    # первая глава ВСЕГДА 0:00 - так требует YouTube, чтобы вообще
    # показать главы под видео
    if lines:
        first_sec, first_text = lines[0]
        if first_sec > 0:
            title_part = first_text.split("—", 1)[-1].strip()
            lines[0] = (0, f"0:00 — {title_part}")
    return "\n".join(text for _, text in lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--blocks-dir", required=True, help="Папка с текстовыми файлами блоков сценария")
    parser.add_argument("--audio-dir", required=True, help="Папка с готовыми mp3 озвучки блоков (результаты)")
    parser.add_argument("--master", required=True, help="MASTER_ПРОМТ_YOUTUBE_ОПИСАНИЕ.txt")
    parser.add_argument("--output", required=True, help="Куда сохранить готовый текст для YouTube")
    parser.add_argument("--model", default="gpt-5.1")
    parser.add_argument("--previous-films", default="",
                         help="Через запятую темы предыдущих фильмов цикла, если есть - "
                              "для фразы-отсылки в конце описания. Пусто - фраза не добавляется.")
    args = parser.parse_args()

    blocks_dir = Path(args.blocks_dir)
    audio_dir = Path(args.audio_dir)
    master_prompt = Path(args.master).read_text(encoding="utf-8")

    block_files = sorted(blocks_dir.glob("*.txt"), key=lambda p: natural_sort_key_from_stem(p.stem))
    if not block_files:
        print(f"ОШИБКА: в {blocks_dir} не найдено .txt файлов")
        sys.exit(1)

    blocks = []
    cumulative = 0.0
    for block_file in block_files:
        block_name = block_file.stem
        audio_path = audio_dir / f"{block_name}.mp3"
        if not audio_path.exists():
            print(f"[!] Нет озвучки для блока '{block_name}' ({audio_path}) - пропускаю его "
                  f"в тайминге (главы туда не попадут).")
            continue
        duration = get_duration_sec(audio_path)
        if duration is None:
            print(f"[!] Не удалось измерить длительность {audio_path} - пропускаю блок '{block_name}'.")
            continue
        text = block_file.read_text(encoding="utf-8").strip()
        blocks.append({
            "name": block_name,
            "start": cumulative,
            "duration": duration,
            "text": text,
        })
        cumulative += duration

    if not blocks:
        print("ОШИБКА: не удалось собрать ни одного блока с озвучкой")
        sys.exit(1)

    total_min = cumulative / 60
    print(f"[i] Блоков с озвучкой: {len(blocks)}, общая длительность: ~{total_min:.1f} мин")

    block_starts = {b["name"]: b["start"] for b in blocks}
    block_durations = {b["name"]: b["duration"] for b in blocks}

    script_text = "\n\n".join(
        f"--- БЛОК: {b['name']} (начинается на {format_timestamp(b['start'])} от начала фильма, "
        f"длительность {format_timestamp(b['duration'])}) ---\n{b['text']}"
        for b in blocks
    )

    previous_films = args.previous_films.strip()
    previous_films_note = (
        f"Предыдущие фильмы цикла (для фразы-отсылки в конце описания): {previous_films}"
        if previous_films else
        "Предыдущих фильмов цикла нет (или не даны) - фразу-отсылку к ним не добавляй вообще."
    )

    user_content = (
        f"Общая длительность фильма: {format_timestamp(cumulative)}\n\n"
        f"{previous_films_note}\n\n"
        f"ПОЛНЫЙ ТЕКСТ СЦЕНАРИЯ ПО БЛОКАМ (в порядке появления в фильме):\n\n{script_text}"
    )

    openai_key = os.environ.get("OPENAI_API_KEY")
    openai_base_url = os.environ.get("OPENAI_BASE_URL")
    if not openai_key:
        print("ОШИБКА: не задан OPENAI_API_KEY")
        sys.exit(1)
    client = OpenAI(api_key=openai_key, base_url=openai_base_url) if openai_base_url \
        else OpenAI(api_key=openai_key)

    print("[i] Отправляю сценарий модели...")
    response = client.chat.completions.create(
        model=args.model,
        max_completion_tokens=8000,
        messages=[
            {"role": "system", "content": master_prompt},
            {"role": "user", "content": user_content},
        ],
    )
    print(f"    finish_reason: {response.choices[0].finish_reason}, "
          f"токенов: {response.usage.total_tokens}")
    raw = strip_wrapping(response.choices[0].message.content)

    sections = parse_sections(raw)
    required = ["TITLE_OPTIONS", "THUMBNAIL_TEXT_OPTIONS", "THUMBNAIL_CONCEPT", "DESCRIPTION_INTRO",
                "DISCLAIMER_MUTANTS", "DISCLAIMER_AI_FOOTAGE", "CHAPTERS", "SOURCES",
                "SHORT_HASHTAGS", "FULL_TAGS"]
    missing = [r for r in required if r not in sections]
    if missing:
        print(f"[!!!] В ответе модели не хватает секций: {', '.join(missing)} - "
              f"результат может быть неполным, смотри сохранённый файл.")

    chapters_text = build_chapters_text(sections.get("CHAPTERS", ""), block_starts, block_durations)
    hashtags = sections.get("SHORT_HASHTAGS", "")
    hashtags_line = " ".join(f"#{t.strip().lstrip('#')}" for t in hashtags.split(",") if t.strip())

    final_description = "\n\n".join(filter(None, [
        sections.get("DESCRIPTION_INTRO", ""),
        sections.get("DISCLAIMER_MUTANTS", ""),
        "⚠️ " + sections.get("DISCLAIMER_AI_FOOTAGE", ""),
        "🕐 Главы:\n" + chapters_text if chapters_text else "",
        "📚 " + sections.get("SOURCES", ""),
        hashtags_line,
    ]))

    output_text = (
        f"=== ЗАГОЛОВОК (варианты) ===\n{sections.get('TITLE_OPTIONS', '')}\n\n"
        f"=== ТЕКСТ НА ПРЕВЬЮ (варианты) ===\n{sections.get('THUMBNAIL_TEXT_OPTIONS', '')}\n\n"
        f"=== КОНЦЕПЦИЯ КАРТИНКИ ПРЕВЬЮ ===\n{sections.get('THUMBNAIL_CONCEPT', '')}\n\n"
        f"=== ОПИСАНИЕ ПОД ВИДЕО (готово, можно вставлять целиком) ===\n{final_description}\n\n"
        f"=== ТЕГИ ДЛЯ YOUTUBE (поле тегов) ===\n{sections.get('FULL_TAGS', '')}\n"
    )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output_text, encoding="utf-8")
    print(f"\nГотово! Сохранено: {output_path}")
    print("[!] Тайминг глав примерный (по реальной длительности озвучки блоков) - "
          "сверь с финальным смонтированным видео перед публикацией. Источники - "
          "проверь/впиши сама, если в тексте не было явных ссылок на исследования.")


if __name__ == "__main__":
    main()
