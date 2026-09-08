"""
Проверяет все CSV в папке результатов: сравнивает последнее значение 'end'
в каждом файле с реальной длительностью соответствующего аудиофайла (.mp3),
и показывает, у каких блоков не хватает кадров (подозрение на обрыв генерации).

Требует ffprobe (обычно уже есть на сервере вместе с ffmpeg) для определения
длительности mp3. Если ffprobe нет - можно передать длительности вручную
через --durations (см. пример ниже).

ИСПОЛЬЗОВАНИЕ (автоматически через ffprobe):
    python3 check_csv_coverage.py --dir результаты

ИСПОЛЬЗОВАНИЕ (если ffprobe недоступен, длительности вручную):
    python3 check_csv_coverage.py --dir результаты --durations "1_блок=253,2_блок=472,3_блок=603"
"""

import argparse
import re
import csv
import subprocess
from pathlib import Path



def natural_sort_key(path):
    """Сортирует файлы по числу в начале имени (1, 2, ..., 10, 11), а не
    по алфавиту как текст (где "10" встаёт перед "2"). Файлы без числа
    в начале (например "хук") идут ПЕРВЫМИ, как вступление перед блоком 1."""
    match = re.match(r"^(\d+)", path.stem)
    if match:
        return (0, int(match.group(1)), path.stem)
    return (-1, 0, path.stem)  # "хук" и подобные - идут ПЕРВЫМИ, как вступление


def get_audio_duration_ffprobe(mp3_path: Path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(mp3_path)],
            capture_output=True, text=True, check=True,
        )
        return round(float(result.stdout.strip()))
    except Exception as e:
        print(f"  [!] Не удалось определить длительность {mp3_path.name} через ffprobe: {e}")
        return None


def get_csv_last_end(csv_path: Path):
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        rows = list(reader)
    if not rows:
        return None, 0
    try:
        last_end = int(rows[-1].get("end", ""))
    except (ValueError, TypeError):
        last_end = None
    return last_end, len(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", required=True, help="Папка с готовыми .csv и .mp3 файлами блоков")
    parser.add_argument("--durations", default=None,
                         help="Длительности вручную, если нет ffprobe: 'имя1=сек,имя2=сек'")
    args = parser.parse_args()

    manual_durations = {}
    if args.durations:
        for pair in args.durations.split(","):
            name, sec = pair.split("=")
            manual_durations[name.strip()] = int(sec.strip())

    result_dir = Path(args.dir)
    csv_files = sorted(result_dir.glob("*.csv"), key=natural_sort_key)

    if not csv_files:
        print(f"В папке {result_dir} не найдено .csv файлов")
        return

    print(f"{'Блок':<20} {'Аудио, сек':<12} {'CSV покрывает':<15} {'Кадров':<8} {'Статус'}")
    print("-" * 75)

    problems = []

    for csv_path in csv_files:
        block_name = csv_path.stem
        mp3_path = csv_path.with_suffix(".mp3")

        if block_name in manual_durations:
            audio_duration = manual_durations[block_name]
        elif mp3_path.exists():
            audio_duration = get_audio_duration_ffprobe(mp3_path)
        else:
            audio_duration = None

        last_end, num_rows = get_csv_last_end(csv_path)

        if audio_duration is None or last_end is None:
            status = "? нет данных для сравнения"
        else:
            diff = audio_duration - last_end
            if diff > 15:  # больше 15 сек не хватает - подозрительно
                status = f"НЕ ХВАТАЕТ ~{diff} сек - ПЕРЕГЕНЕРИРОВАТЬ"
                problems.append(block_name)
            else:
                status = "OK"

        print(f"{block_name:<20} {str(audio_duration or '?'):<12} "
              f"{str(last_end or '?'):<15} {num_rows:<8} {status}")

    print("\n" + "=" * 75)
    if problems:
        print(f"Нужно перегенерировать ({len(problems)}): {', '.join(problems)}")
    else:
        print("Все проверенные блоки в порядке!")


if __name__ == "__main__":
    main()
