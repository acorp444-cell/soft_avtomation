"""
Автоматически собирает готовое видео ОДНОГО БЛОКА из уже сгенерированных
картинок/видео и озвучки. video1/video2 генерируются с фиксированной
длиной 8 секунд (Veo) - поэтому правила зависят от того, насколько кадр
превышает эти 8 секунд:

  - кадр <= 4 сек:            статичная картинка img1, эффект Кена Бёрнса
  - 4 сек < кадр <= 8 сек:    только video1, обрезаем до нужной длины
  - 8 сек < кадр <= 10 сек:   только video1, ЗАМЕДЛЯЕМ (растягиваем по
                                скорости), чтобы растянуть ровно до нужной
                                длины - нехватка маленькая (1-2 сек), для
                                неё это менее заметно, чем заморозка кадра
  - 10 сек < кадр <= 12 сек:  video1 (целиком, без замедления) + img2
                                (Кен Бёрнс) на остаток
  - кадр > 12 сек:            video1 (целиком) + video2, обрезаем/
                                дозаполняем стоп-кадром на остаток

Кен Бёрнс на картинках всегда использует масштаб 104% -> 115%. Видео-
кадры (обрезка/переприкодирование) всегда используют масштаб 108% с
обрезкой по центру - убирает чёрные рамки при несовпадении пропорций.

Замедление применяется ТОЛЬКО в узком диапазоне 8-10 секунд (по явному
указанию) - во всех остальных случаях действует правило "только обрезка,
никогда не ускорение/замедление". Если видео короче нужного даже вдвоём -
остаток заполняется "заморозкой" последнего кадра второго видео.

Финальное видео блока = склеенные кадры (без звука) + наложенная поверх
непрерывная озвучка блока (mp3) - монтаж кадр в кадр под уже точный
тайминг из CSV.

ИСПОЛЬЗОВАНИЕ:
    python3 assemble_block_video.py --csv результаты/1_блок.csv \
        --audio результаты/1_блок.mp3 --media-dir ../output \
        --output результаты/1_блок_edit.mp4

ТРЕБОВАНИЯ: ffmpeg и ffprobe должны быть установлены на сервере.
"""

import argparse
import csv
import glob
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

WIDTH, HEIGHT, FPS = 1920, 1080, 25

# По умолчанию - обычное программное кодирование (работает везде, в том
# числе на RunPod, где нет Intel-видеокарты). На слабом ПК можно включить
# --hw-encoder qsv/nvenc/amf, если ffmpeg такое поддерживает - тогда
# кодирование считает видеокарта, а не процессор, и работает намного быстрее.
VIDEO_CODEC = "libx264"
EXTRA_ENCODE_ARGS = []

HW_ENCODER_QUALITY_ARGS = {
    "qsv": ["-global_quality", "23"],
    "nvenc": ["-preset", "p4", "-cq", "23"],
    "amf": ["-quality", "balanced", "-qp_i", "23", "-qp_p", "23"],
}


def _codec_args():
    return ["-c:v", VIDEO_CODEC] + EXTRA_ENCODE_ARGS

# Принудительно делаем вывод построчным (без буферизации). По умолчанию,
# когда Python печатает не в настоящий терминал, а в трубу (как при
# запуске через SSH), он копит вывод пачками по несколько КБ перед
# отправкой - из-за этого журнал в приложении выглядит "зависшим" на
# много минут, хотя скрипт реально работает и печатает, просто буфер
# ещё не заполнился настолько, чтобы сброситься.
sys.stdout.reconfigure(line_buffering=True)

TRANSITION_DURATION = 0.5  # длительность плавного перехода между кадрами ("микс" в CapCut)
VIDEO_OVERSCAN = 1.08  # масштаб 108% с обрезкой по центру - убирает чёрные рамки
_OVERSCAN_W = int(WIDTH * VIDEO_OVERSCAN)
_OVERSCAN_H = int(HEIGHT * VIDEO_OVERSCAN)
SCALE_CROP_VF = (
    f"scale={_OVERSCAN_W}:{_OVERSCAN_H}:force_original_aspect_ratio=increase,"
    f"crop={WIDTH}:{HEIGHT},fps={FPS},format=yuv420p"
)


def run_ffmpeg(args, description=""):
    cmd = ["ffmpeg", "-y", "-loglevel", "error"] + args
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[!] Ошибка ffmpeg ({description}): {result.stderr.strip()[-500:]}")
        return False
    return True


def get_duration(path: Path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def find_image(media_dirs, num: str, which: str):
    """Ищет финальную (апскейленную) картинку - ComfyUI добавляет свой
    счётчик к имени файла, поэтому ищем по маске. Перебирает все переданные
    папки по очереди (картинки и видео могут лежать в разных папках)."""
    for media_dir in media_dirs:
        pattern = str(media_dir / f"{num}_{which}_*.png")
        matches = sorted(glob.glob(pattern))
        if matches:
            return Path(matches[-1])
        # запасной вариант - файл без счётчика
        exact = media_dir / f"{num}_{which}.png"
        if exact.exists():
            return exact
    return None


def find_video(media_dirs, num: str, which: str):
    for media_dir in media_dirs:
        path = media_dir / f"{num}_{which}_video.mp4"
        if path.exists():
            return path
    return None


def build_kenburns_clip(image_path: Path, duration_sec: float, output_path: Path,
                         start_zoom: float = 1.04, end_zoom: float = 1.15):
    """Создаёт клип из статичной картинки с плавным зумом (эффект Кена
    Бёрнса): старт start_zoom (104%), к концу клипа - end_zoom (115%),
    линейно, независимо от длительности клипа."""
    frames = max(1, round(duration_sec * FPS))
    zoom_per_frame = (end_zoom - start_zoom) / frames
    vf = (
        # 3840 (2х ширины итогового видео) с запасом хватает для плавного
        # зума 104%-115% без пикселизации - 8000 только сильно грузил
        # zoompan (и так один из самых медленных фильтров ffmpeg) без
        # заметной разницы в качестве
        f"scale=3840:-1,"
        f"zoompan=z='{start_zoom}+{zoom_per_frame}*on':d={frames}:"
        f"s={WIDTH}x{HEIGHT}:fps={FPS},format=yuv420p"
    )
    return run_ffmpeg([
        "-loop", "1", "-i", str(image_path),
        "-vf", vf, "-t", str(duration_sec),
        *_codec_args(), "-pix_fmt", "yuv420p",
        str(output_path),
    ], f"Ken Burns {image_path.name}")


def trim_video(input_path: Path, duration_sec: float, output_path: Path):
    """Обрезает видео до нужной длины (никогда не ускоряет), приводит к
    единому формату для последующей склейки. Масштаб 108% с обрезкой по
    центру - убирает чёрные рамки при небольшом несовпадении пропорций."""
    return run_ffmpeg([
        "-i", str(input_path), "-t", str(duration_sec),
        "-vf", SCALE_CROP_VF,
        *_codec_args(), "-an",
        str(output_path),
    ], f"обрезка {input_path.name}")


def full_video_reencoded(input_path: Path, output_path: Path):
    """Переприводит видео целиком к единому формату, без обрезки по
    времени. Масштаб 108% с обрезкой по центру - убирает чёрные рамки."""
    return run_ffmpeg([
        "-i", str(input_path),
        "-vf", SCALE_CROP_VF,
        *_codec_args(), "-an",
        str(output_path),
    ], f"перекодирование {input_path.name}")


def stretch_video_to_duration(input_path: Path, target_duration: float, output_path: Path):
    """Замедляет видео (без изменения контента, только скорость), чтобы
    растянуть его РОВНО до target_duration. Используется только для
    небольшой нехватки (кадр 8-10 сек, video1/video2 короче на 1-2 сек) -
    в этом узком случае лёгкое замедление менее заметно, чем "заморозка"
    последнего кадра на пару секунд. Масштаб 108% с обрезкой по центру -
    как и у остальных видео-клипов."""
    actual_dur = get_duration(input_path)
    if not actual_dur or actual_dur <= 0:
        return False
    factor = target_duration / actual_dur  # > 1 = замедление
    vf = (
        f"setpts={factor}*PTS,"
        f"scale={_OVERSCAN_W}:{_OVERSCAN_H}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},fps={FPS},format=yuv420p"
    )
    return run_ffmpeg([
        "-i", str(input_path),
        "-vf", vf,
        *_codec_args(), "-an",
        str(output_path),
    ], f"замедление {input_path.name} до {target_duration:.1f} сек")


def freeze_last_frame_clip(input_path: Path, freeze_duration: float, output_path: Path):
    """Берёт последний кадр видео и делает из него статичный клип нужной длины."""
    with tempfile.TemporaryDirectory() as tmp:
        last_frame = Path(tmp) / "last_frame.png"
        ok = run_ffmpeg([
            "-sseof", "-0.1", "-i", str(input_path),
            "-frames:v", "1", str(last_frame),
        ], "извлечение последнего кадра")
        if not ok or not last_frame.exists():
            return False
        return run_ffmpeg([
            "-loop", "1", "-i", str(last_frame),
            "-t", str(freeze_duration),
            "-vf", SCALE_CROP_VF,
            *_codec_args(),
            str(output_path),
        ], "заморозка кадра")


def _make_clip_from_video(source_path: Path, target_duration: float, work_dir: Path, tag: str):
    """Готовит клип из видео нужной длины: обрезает если видео длиннее,
    дозаполняет заморозкой последнего кадра если короче. Никогда не ускоряет."""
    actual_dur = get_duration(source_path) or 0
    trimmed = min(target_duration, actual_dur) if actual_dur > 0 else target_duration
    main_clip = work_dir / f"{tag}_main.mp4"
    if not trim_video(source_path, trimmed, main_clip):
        return None
    if trimmed >= target_duration - 0.2:
        return main_clip
    remaining = target_duration - trimmed
    freeze_clip = work_dir / f"{tag}_freeze.mp4"
    if freeze_last_frame_clip(source_path, remaining, freeze_clip):
        return concat_clips([main_clip, freeze_clip], work_dir / f"{tag}_full.mp4")
    return main_clip


def build_frame_clip(media_dirs, num: str, frame_duration: int, work_dir: Path, index: int):
    """Собирает клип для одного кадра. Приоритет всегда за видео, картинка -
    только если видео физически нет. Если кадр длинный и нужны ДВЕ части,
    разрешены только пары: video1+video2, video1+img2, video2+img1,
    img1+img2 - НИКОГДА video1+img1 или video2+img2 (картинка - по сути
    тот же кадр, что и видео с тем же номером, повтор одной сцены)."""
    out_path = work_dir / f"frame_{index:04d}.mp4"
    tag = f"frame_{index:04d}"

    video1 = find_video(media_dirs, num, "img1")
    video2 = find_video(media_dirs, num, "img2")
    img1 = find_image(media_dirs, num, "img1")
    img2 = find_image(media_dirs, num, "img2")

    # --- короткий кадр (<=4 сек): всегда картинка с Кеном Бёрнсом ---
    if frame_duration <= 4:
        chosen_img = img1 or img2
        if not chosen_img:
            print(f"[!] Кадр {num}: нет ни одной картинки для короткого кадра ({frame_duration} сек)")
            return None
        return out_path if build_kenburns_clip(chosen_img, frame_duration, out_path) else None

    # --- выбираем "первую" часть: video1 -> video2 -> img1 -> img2 (приоритет видео) ---
    if video1:
        primary_kind, primary_path = "video1", video1
    elif video2:
        primary_kind, primary_path = "video2", video2
    elif img1:
        primary_kind, primary_path = "img1", img1
    elif img2:
        primary_kind, primary_path = "img2", img2
    else:
        print(f"[!] Кадр {num}: вообще нет материала (ни видео, ни картинок)")
        return None

    # если единственное, что есть - картинка (видео нет совсем) - Кен Бёрнс на весь кадр
    if primary_kind in ("img1", "img2"):
        other_img = img2 if primary_kind == "img1" else img1
        if other_img and frame_duration > 8:
            # разрешённая пара img1+img2 - делим кадр пополам для разнообразия
            half = frame_duration / 2
            clip_a = work_dir / f"{tag}_a.mp4"
            clip_b = work_dir / f"{tag}_b.mp4"
            if build_kenburns_clip(primary_path, half, clip_a) and build_kenburns_clip(other_img, frame_duration - half, clip_b):
                return concat_clips([clip_a, clip_b], out_path)
        return out_path if build_kenburns_clip(primary_path, frame_duration, out_path) else None

    # --- primary - видео. Определяем "противоположные" video/img по номеру ---
    if primary_kind == "video1":
        other_video, other_video_kind = video2, "video2"
        allowed_img, allowed_img_kind = img2, "img2"  # video1 никогда не с img1
    else:
        other_video, other_video_kind = video1, "video1"
        allowed_img, allowed_img_kind = img1, "img1"  # video2 никогда не с img2

    primary_dur = get_duration(primary_path) or 0

    # <= 8 сек: только primary-видео, просто обрезаем до нужной длины
    if frame_duration <= 8:
        result = _make_clip_from_video(primary_path, frame_duration, work_dir, tag)
        return result if result else None

    # 8-10 сек: небольшая нехватка (1-2 сек по сравнению с 8-секундным
    # video1/video2) - не подменяем картинкой/вторым видео, а слегка
    # ЗАМЕДЛЯЕМ primary, чтобы растянуть ровно до нужной длины (только в
    # этом узком диапазоне - по явному указанию)
    if frame_duration <= 10:
        stretched_clip = work_dir / f"{tag}_stretched.mp4"
        if stretch_video_to_duration(primary_path, frame_duration, stretched_clip):
            return stretched_clip
        # не получилось замедлить - запасной вариант, как раньше (обрезка/заморозка)
        result = _make_clip_from_video(primary_path, frame_duration, work_dir, tag)
        return result if result else None

    # больше 10 сек: primary целиком (без замедления) + вторая часть на остаток
    primary_clip = work_dir / f"{tag}_primary.mp4"
    if not full_video_reencoded(primary_path, primary_clip):
        return None
    remaining = max(0.5, frame_duration - primary_dur)

    if frame_duration <= 12:
        # 10-12 сек: остаток небольшой (2-4 сек) - добираем картинкой (Кен Бёрнс)
        preferred, fallback = allowed_img, other_video
        preferred_is_img = True
    else:
        # больше 12 сек: остаток заметный - добираем ВТОРЫМ ВИДЕО
        preferred, fallback = other_video, allowed_img
        preferred_is_img = False

    if preferred:
        if preferred_is_img:
            img_clip = work_dir / f"{tag}_img.mp4"
            if build_kenburns_clip(preferred, remaining, img_clip):
                return concat_clips([primary_clip, img_clip], out_path)
        else:
            second_clip = _make_clip_from_video(preferred, remaining, work_dir, f"{tag}_second")
            if second_clip:
                return concat_clips([primary_clip, second_clip], out_path)

    if fallback:
        if preferred_is_img:
            second_clip = _make_clip_from_video(fallback, remaining, work_dir, f"{tag}_second")
            if second_clip:
                return concat_clips([primary_clip, second_clip], out_path)
        else:
            img_clip = work_dir / f"{tag}_img.mp4"
            if build_kenburns_clip(fallback, remaining, img_clip):
                return concat_clips([primary_clip, img_clip], out_path)

    # крайний случай - ни второго видео, ни разрешённой картинки нет -
    # дозаполняем заморозкой самого primary (лучше так, чем ничего)
    freeze_clip = work_dir / f"{tag}_freeze.mp4"
    if freeze_last_frame_clip(primary_path, remaining, freeze_clip):
        return concat_clips([primary_clip, freeze_clip], out_path)
    return primary_clip


def concat_clips(clip_paths, output_path: Path):
    """Склеивает несколько клипов (уже в едином формате) в один файл -
    ЖЁСТКАЯ склейка без перехода, используется для частей ВНУТРИ одного
    кадра (например video1+img2), не для склейки кадров между собой."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        for p in clip_paths:
            f.write(f"file '{p.resolve()}'\n")
        list_path = f.name
    try:
        ok = run_ffmpeg([
            "-f", "concat", "-safe", "0", "-i", list_path,
            "-c", "copy", str(output_path),
        ], "склейка частей кадра")
        if not ok:
            # запасной путь - переклеить с перекодированием, если copy не сработал
            ok = run_ffmpeg([
                "-f", "concat", "-safe", "0", "-i", list_path,
                *_codec_args(), "-pix_fmt", "yuv420p", str(output_path),
            ], "склейка частей кадра (перекодирование)")
        return output_path if ok else None
    finally:
        os.unlink(list_path)


def xfade_pair(clip_a: Path, duration_a: float, clip_b: Path, transition_duration: float, output_path: Path):
    """Склеивает два клипа с плавным переходом ('микс') длительностью
    transition_duration. offset - момент в clip_a, с которого начинается
    переход (в конце clip_a)."""
    offset = max(0.0, duration_a - transition_duration)
    # явно нормализуем fps/timebase обоих клипов перед xfade - иначе он
    # спотыкается о несовпадение параметров даже при одинаковом fps
    filter_complex = (
        f"[0:v]fps={FPS},format=yuv420p,setpts=PTS-STARTPTS[v0];"
        f"[1:v]fps={FPS},format=yuv420p,setpts=PTS-STARTPTS[v1];"
        f"[v0][v1]xfade=transition=fade:duration={transition_duration}:offset={offset},format=yuv420p[vout]"
    )
    return run_ffmpeg([
        "-i", str(clip_a), "-i", str(clip_b),
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        *_codec_args(),
        str(output_path),
    ], f"переход между кадрами")


def concat_clips_with_crossfade(frame_clips_with_durations, transition_duration: float, work_dir: Path, output_path: Path):
    """Склеивает кадры блока с плавными переходами между НИМИ (не путать с
    concat_clips, которая склеивает части ВНУТРИ одного кадра - там переход
    не нужен). Длительность на каждом шаге ИЗМЕРЯЕТСЯ через ffprobe, а не
    вычисляется теоретически - это исключает накопление ошибок округления.

    frame_clips_with_durations - список (путь_к_клипу, ...любые доп. поля,
    не используются здесь)."""
    if len(frame_clips_with_durations) == 1:
        return frame_clips_with_durations[0][0]

    total_steps = len(frame_clips_with_durations) - 1
    # "Чистая" сумма длительностей кадров (без добавок на переход) - к
    # этому числу должно прийти итоговое видео, используем как ориентир
    # для процента прогресса (не точный процент, но честная оценка).
    expected_total = sum(d[2] for d in frame_clips_with_durations)

    running_clip = frame_clips_with_durations[0][0]
    running_duration = get_duration(running_clip)
    if running_duration is None:
        print(f"[!] Не удалось измерить длительность {running_clip}")
        return None

    for i in range(1, len(frame_clips_with_durations)):
        clip_b = frame_clips_with_durations[i][0]

        if expected_total:
            percent = min(99, int(running_duration * 100 / expected_total))
            print(f"  Переход {i}/{total_steps}: склеено ~{running_duration:.0f} сек ({percent}%)...")
        else:
            print(f"  Переход {i}/{total_steps}...")

        merged_path = work_dir / f"merged_{i:04d}.mp4"
        ok = xfade_pair(running_clip, running_duration, clip_b, transition_duration, merged_path)
        if not ok:
            print(f"[!] Не удалось сделать переход на кадре {i} - склеиваю жёстко, без перехода")
            merged_path = work_dir / f"merged_hard_{i:04d}.mp4"
            if not concat_clips([running_clip, clip_b], merged_path):
                return None

        running_clip = merged_path
        running_duration = get_duration(running_clip)
        if running_duration is None:
            print(f"[!] Не удалось измерить длительность промежуточного файла на шаге {i}")
            return None

    shutil.copy(str(running_clip), str(output_path))
    return output_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True, help="CSV-файл блока")
    parser.add_argument("--audio", required=True, help="Полный mp3 озвучки этого блока")
    parser.add_argument("--media-dir", required=True, help="Папка с готовыми картинками/видео (ComfyUI/output)")
    parser.add_argument("--media-dir2", required=False, default=None,
                         help="Доп. папка для поиска картинок/видео (например, если картинки и "
                              "видео скачаны в разные папки - сначала ищем в --media-dir, потом тут)")
    parser.add_argument("--output", required=True, help="Куда сохранить готовое видео блока")
    parser.add_argument("--hw-encoder", choices=["none", "qsv", "nvenc", "amf"], default="none",
                         help="Кодировать через видеокарту вместо процессора (сильно быстрее, если "
                              "поддерживается) - none/qsv (Intel)/nvenc (NVIDIA)/amf (AMD)")
    args = parser.parse_args()

    if args.hw_encoder != "none":
        global VIDEO_CODEC, EXTRA_ENCODE_ARGS
        VIDEO_CODEC = f"h264_{args.hw_encoder}"
        EXTRA_ENCODE_ARGS = HW_ENCODER_QUALITY_ARGS[args.hw_encoder]
        print(f"[i] Аппаратное кодирование включено: {VIDEO_CODEC}")

    csv_path = Path(args.csv)
    audio_path = Path(args.audio)
    media_dirs = [Path(args.media_dir)]
    if args.media_dir2:
        media_dirs.append(Path(args.media_dir2))
    output_path = Path(args.output)

    if not csv_path.exists():
        print(f"ОШИБКА: CSV не найден: {csv_path}")
        sys.exit(1)
    if not audio_path.exists():
        print(f"ОШИБКА: аудио не найдено: {audio_path}")
        sys.exit(1)

    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        rows = list(reader)

    print(f"Кадров в блоке: {len(rows)}")

    with tempfile.TemporaryDirectory() as tmp_dir_str:
        work_dir = Path(tmp_dir_str)
        frame_clips_with_durations = []

        valid_rows = []
        for row in rows:
            try:
                start = int(row.get("start", "0"))
                end = int(row.get("end", "0"))
            except ValueError:
                print(f"[!] Строка с некорректными start/end, пропускаю: {row}")
                continue
            duration = end - start
            if duration <= 0:
                continue
            valid_rows.append((row.get("num", "").strip(), duration))

        def _num_sort_key(item):
            num_str = item[0]
            try:
                return (0, int(num_str))
            except ValueError:
                return (1, num_str)  # некорректный/нечисловой номер - в конец

        original_order = [n for n, _ in valid_rows]
        valid_rows.sort(key=_num_sort_key)
        sorted_order = [n for n, _ in valid_rows]
        if original_order != sorted_order:
            print(f"[!] Строки CSV были не по порядку num - пересортировала "
                  f"перед сборкой (было: {original_order}, стало: {sorted_order})")

        for i, (num, duration) in enumerate(valid_rows):
            is_last = (i == len(valid_rows) - 1)

            print(f"[{i + 1}/{len(valid_rows)}] Кадр {num}, {duration} сек...")
            clip = build_frame_clip(media_dirs, num, duration, work_dir, i)
            if clip is None:
                print(f"[!!!] Кадр {num} не удалось собрать - видео будет короче озвучки в этом месте")
                continue

            actual_duration = get_duration(clip) or duration

            if not is_last:
                # добавляем ТОЧНО TRANSITION_DURATION секунд отдельным шагом
                # (не через build_frame_clip - там могло бы округлиться) -
                # этот довесок "съест" сам переход при склейке
                extended_path = work_dir / f"frame_{i:04d}_extended.mp4"
                if freeze_last_frame_clip(clip, TRANSITION_DURATION, work_dir / f"frame_{i:04d}_pad.mp4") and \
                        concat_clips([clip, work_dir / f"frame_{i:04d}_pad.mp4"], extended_path):
                    clip = extended_path
                    actual_duration = actual_duration + TRANSITION_DURATION
                else:
                    print(f"[!] Кадр {num}: не удалось добавить запас на переход - "
                          f"склею этот стык жёстко, без перехода")

            frame_clips_with_durations.append((clip, actual_duration, duration))

        if not frame_clips_with_durations:
            print("ОШИБКА: не собрано ни одного кадра")
            sys.exit(1)

        print(f"\nСклеиваю {len(frame_clips_with_durations)} кадров с переходами "
              f"({TRANSITION_DURATION} сек, 'микс')...")
        video_no_audio = work_dir / "video_no_audio.mp4"
        result = concat_clips_with_crossfade(frame_clips_with_durations, TRANSITION_DURATION,
                                              work_dir, video_no_audio)
        if result is None:
            print("ОШИБКА: не удалось склеить кадры")
            sys.exit(1)

        print("Накладываю озвучку...")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        ok = run_ffmpeg([
            "-i", str(video_no_audio), "-i", str(audio_path),
            "-c:v", "copy", "-c:a", "aac", "-shortest",
            str(output_path),
        ], "наложение звука")

        if not ok:
            print("ОШИБКА: не удалось наложить звук")
            sys.exit(1)

    print(f"\nГотово! Видео блока сохранено: {output_path}")


if __name__ == "__main__":
    main()
