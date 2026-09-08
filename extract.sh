#!/bin/bash
# Использование:
#   Диапазон:        ./extract.sh 315-316 [--run]
#   Список номеров:  ./extract.sh 3,4,8,9,12,15 [--run]
#
# Автоматически ищет строки с нужными num среди ВСЕХ csv-файлов в папке
# (1_block.csv...9_block.csv, final.csv, hook.csv и любых других *.csv),
# кроме уже созданных temp_*.csv — искать вручную, где что лежит, не нужно.
#
# Примеры:
#   ./extract.sh 315-316
#   ./extract.sh 3,4,8,9,10,11,12,14,15 --run

set -e

NUMS="$1"
RUN_FLAG="$2"

if [ -z "$NUMS" ]; then
    echo "Использование:"
    echo "  Диапазон:       ./extract.sh 315-316 [--run]"
    echo "  Список номеров: ./extract.sh 3,4,8,9,12 [--run]"
    exit 1
fi

# Все csv-файлы в папке, кроме temp_*.csv (наши же промежуточные файлы)
CSV_FILES=()
for f in *.csv; do
    [[ "$f" == temp_* ]] && continue
    CSV_FILES+=("$f")
done

if [ ${#CSV_FILES[@]} -eq 0 ]; then
    echo "Не найдено ни одного исходного csv-файла в текущей папке."
    exit 1
fi

OUT_FILE="temp_$(echo "$NUMS" | tr ',' '_' | tr '-' 'to').csv"

# Берём заголовок из первого попавшегося файла
head -1 "${CSV_FILES[0]}" > "$OUT_FILE"

if [[ "$NUMS" == *","* ]]; then
    MODE="list"
elif [[ "$NUMS" == *"-"* ]]; then
    MODE="range"
    FROM="${NUMS%-*}"
    TO="${NUMS#*-}"
else
    MODE="single"
fi

for f in "${CSV_FILES[@]}"; do
    if [ "$MODE" == "list" ]; then
        awk -F';' -v nums="$NUMS" '
            BEGIN { n = split(nums, arr, ","); for (i=1;i<=n;i++) want[arr[i]+0]=1 }
            NR>1 && ($1+0 in want)
        ' "$f" >> "$OUT_FILE"
    elif [ "$MODE" == "range" ]; then
        awk -F';' -v from="$FROM" -v to="$TO" 'NR>1 && $1+0 >= from && $1+0 <= to' "$f" >> "$OUT_FILE"
    else
        awk -F';' -v n="$NUMS" 'NR>1 && $1+0 == n' "$f" >> "$OUT_FILE"
    fi
done

LINES_FOUND=$(($(wc -l < "$OUT_FILE") - 1))

echo "-----------------------------------"
echo "Создан файл: $OUT_FILE"
echo "Найдено строк данных: $LINES_FOUND"
echo "-----------------------------------"
cat "$OUT_FILE"
echo "-----------------------------------"

if [ "$LINES_FOUND" -le 0 ]; then
    echo "[!] Ничего не найдено ни в одном csv-файле для: $NUMS"
    exit 1
fi

if [ "$RUN_FLAG" == "--run" ]; then
    echo "[i] Запускаю генерацию..."
    python3 generate_via_api_and_upscale.py --run --csv "$OUT_FILE"
else
    echo "[i] Файл готов. Чтобы запустить генерацию, выполни:"
    echo "    python3 generate_via_api_and_upscale.py --run --csv $OUT_FILE"
fi
