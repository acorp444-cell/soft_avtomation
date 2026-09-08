"""
Нарезает ПОЛНУЮ озвучку блока на отдельные аудио-кусочки, по одному на
каждую строку (кадр) CSV, используя её start/end. Каждый кусочек
называется по номеру кадра (num) - совпадает с именами уже готовых
картинок/видео (133_img1.png, 133_video.mp4), чтобы было легко сопоставить
в CapCut.

ТРЕБОВАНИЯ: ffmpeg должен быть установлен на сервере (обычно уже есть).

ИСПОЛЬЗОВАНИЕ:
    python3 split_audio_by_csv.py --csv результаты/4_block.csv --audio результаты/4_block.mp3 --output-dir результаты/4_block_audio
"""

import argparse
import csv
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True, help="CSV-файл блока (с колонками num;start;end;...)")
    parser.add_argument("--audio", required=True, help="Полный mp3-файл озвучки этого же блока")
    parser.add_argument("--output-dir", required=True, help="Куда сохранить нарезанные кусочки")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    audio_path = Path(args.audio)
    output_dir = Path(args.output_dir)

    if not csv_path.exists():
        print(f"ОШИБКА: CSV не найден: {csv_path}")
        sys.exit(1)
    if not audio_path.exists():
        print(f"ОШИБКА: аудио не найдено: {audio_path}")
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        rows = list(reader)

    print(f"Найдено кадров: {len(rows)}")
    ok_count = 0

    for row in rows:
        num = row.get("num", "").strip()
        start = row.get("start", "").strip()
        end = row.get("end", "").strip()

        if not num or not start or not end:
            print(f"[-] Пропускаю строку без num/start/end: {row}")
            continue

        try:
            start_sec = float(start)
            end_sec = float(end)
        except ValueError:
            print(f"[-] Строка {num}: не удалось разобрать start/end ({start}, {end})")
            continue

        duration = end_sec - start_sec
        if duration <= 0:
            print(f"[-] Строка {num}: некорректная длительность ({duration} сек)")
            continue

        output_path = output_dir / f"{num}.mp3"

        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(audio_path),
            "-ss", str(start_sec),
            "-t", str(duration),
            "-acodec", "libmp3lame", "-q:a", "2",
            str(output_path),
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"[!] Ошибка на кадре {num}: {result.stderr.strip()}")
            continue

        ok_count += 1
        print(f"[+] {num}.mp3 ({start_sec:.0f}-{end_sec:.0f} сек, {duration:.1f} сек)")

    print(f"\nГотово! Нарезано {ok_count} из {len(rows)} кусочков в {output_dir}")


if __name__ == "__main__":
    main()
