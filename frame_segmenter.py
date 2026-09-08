"""
Детерминированно (без ИИ, бесплатно) делит текст блока на кадры:
  1. Считает слова в тексте и целевое число кадров (по длительности блока
     и целевой длине кадра, например ~9 секунд)
  2. Идёт по предложениям, накапливая слова, пока не наберётся целевое
     количество на кадр - ЗАКРЫВАЕТ КАДР ТОЛЬКО НА ГРАНИЦЕ ПРЕДЛОЖЕНИЯ,
     никогда не разрезая фразу пополам
  3. Вычисляет start/end каждого кадра пропорционально доле слов от общей
     длительности (не поровну, а по факту объёма текста в кадре)

Результат - список кадров с уже готовыми num/start/end/duration/
voiceover_ru, гарантированно без пропусков и без дублей (эта часть
вообще не проходит через ИИ).
"""

import re


def natural_sort_key_from_stem(stem: str):
    """Сортирует по числу в начале имени файла (1, 2, ..., 10, 11), а не
    по алфавиту как текст (где "10" встаёт перед "2"). Имена без числа в
    начале (например "хук") идут ПЕРВЫМИ, как вступление перед блоком 1."""
    match = re.match(r"^(\d+)", stem)
    if match:
        return (0, int(match.group(1)), stem)
    return (-1, 0, stem)


def split_sentences(text: str):
    """Разбивает текст на предложения по точке/!/?, с фильтрацией пустых."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def split_long_sentence(sentence: str, max_words: int):
    """Если предложение намного длиннее допустимого кадра - режет его по
    естественным паузам (запятая, тире, точка с запятой, двоеточие),
    а не только по точке. Каждый кусок остаётся дословным (просто более
    короткая часть исходного предложения), ничего не теряется и не
    меняется - просто дробится по знакам паузы, как монтажёр режет "по
    дыханию" внутри длинной фразы."""
    words = sentence.split()
    if len(words) <= max_words:
        return [sentence]

    # ищем позиции естественных пауз внутри предложения
    pause_pattern = re.compile(r'(?<=[,;:—])\s+')
    pieces = pause_pattern.split(sentence)
    if len(pieces) <= 1:
        return [sentence]  # нет пауз - оставляем как есть, длинным

    # группируем кусочки по max_words, не разрывая уже сами кусочки
    result = []
    current = []
    current_words = 0
    for piece in pieces:
        piece_words = len(piece.split())
        if current and current_words + piece_words > max_words:
            result.append(" ".join(current))
            current = []
            current_words = 0
        current.append(piece)
        current_words += piece_words
    if current:
        result.append(" ".join(current))
    return result


def build_chunks(text: str, words_per_frame: float):
    """Строит список текстовых кусочков для группировки в кадры: обычные
    предложения целиком, а слишком длинные (намного больше words_per_frame)
    - разбитые по естественным паузам."""
    sentences = split_sentences(text)
    max_words_per_piece = max(1, round(words_per_frame * 1.3))  # небольшой запас

    chunks = []
    for s in sentences:
        if len(s.split()) > max_words_per_piece * 1.5:
            chunks.extend(split_long_sentence(s, max_words_per_piece))
        else:
            chunks.append(s)
    return chunks


def segment_text_into_frames(text: str, duration_sec: int, start_num: int = 1,
                              target_frame_sec: float = 9.0):
    """Делит текст на кадры. Возвращает список словарей:
    {num, start, end, duration, voiceover_ru}."""
    sentences = split_sentences(text)
    if not sentences:
        return []

    total_words = sum(len(s.split()) for s in sentences)
    if total_words == 0:
        return []

    # целевое число кадров исходя из длительности блока
    target_frame_count = max(1, round(duration_sec / target_frame_sec))
    words_per_frame = total_words / target_frame_count

    # строим кусочки: обычные предложения целиком + длинные предложения,
    # раздробленные по естественным паузам (запятые, тире и т.п.)
    chunks = build_chunks(text, words_per_frame)

    # группируем кусочки в кадры по целевому количеству слов - на каждом
    # шаге сравниваем, что ближе к цели: закрыть кадр сейчас или сначала
    # добавить ещё один кусочек (чтобы не "перебирать" объём кадра)
    frame_texts = []
    current = []
    current_words = 0
    for idx, chunk in enumerate(chunks):
        chunk_words = len(chunk.split())
        remaining_frames_needed = target_frame_count - len(frame_texts)

        if current and remaining_frames_needed > 1:
            dist_without = abs(current_words - words_per_frame)
            dist_with = abs(current_words + chunk_words - words_per_frame)
            if dist_with > dist_without:
                # добавление следующего кусочка уводит дальше от цели -
                # закрываем кадр сейчас, кусочек уйдёт в следующий кадр
                frame_texts.append(" ".join(current))
                current = []
                current_words = 0

        current.append(chunk)
        current_words += chunk_words

    if current:
        frame_texts.append(" ".join(current))

    # считаем start/end пропорционально доле слов каждого кадра от общего
    # количества слов (более точная оценка тайминга речи, чем поровну)
    word_counts = [len(ft.split()) for ft in frame_texts]
    total_frame_words = sum(word_counts)

    frames = []
    cumulative_words = 0
    prev_end = 0
    for i, (ft, wc) in enumerate(zip(frame_texts, word_counts)):
        cumulative_words += wc
        is_last = (i == len(frame_texts) - 1)
        if is_last:
            end = duration_sec
        else:
            end = round(duration_sec * cumulative_words / total_frame_words)
        start = prev_end
        duration = end - start
        frames.append({
            "num": start_num + i,
            "start": start,
            "end": end,
            "duration": duration,
            "voiceover_ru": ft,
        })
        prev_end = end

    return frames


if __name__ == "__main__":
    # маленькая самопроверка при прямом запуске файла
    sample_text = (
        "Это первое предложение примера. Это второе, чуть подлиннее предложение для теста. "
        "Третье совсем короткое. Четвёртое предложение снова длиннее, чем предыдущее, для разнообразия. "
        "Пятое и последнее предложение в этом маленьком примере."
    )
    frames = segment_text_into_frames(sample_text, duration_sec=45, start_num=1, target_frame_sec=9)
    for f in frames:
        print(f["num"], f["start"], f["end"], f["duration"], "->", f["voiceover_ru"])
    total_covered = " ".join(f["voiceover_ru"] for f in frames)
    normalized_original = re.sub(r"\s+", " ", sample_text).strip()
    print("\nПокрытие совпадает:", total_covered.strip() == normalized_original)
