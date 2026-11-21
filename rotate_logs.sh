#!/bin/bash
cd "$(dirname "$0")"
mkdir -p logs_archive
for f in *_log.txt; do
  [ -f "$f" ] || continue
  size=$(stat -f%z "$f")
  if [ "$size" -gt 5242880 ]; then
    stamp=$(date +%Y%m%d)
    gzip -c "$f" > "logs_archive/${f%.txt}_$stamp.txt.gz" && : > "$f"
    echo "$(date '+%d.%m %H:%M') $f: $((size / 1048576)) МБ -> logs_archive/${f%.txt}_$stamp.txt.gz"
  fi
  ls -1t logs_archive/${f%.txt}_*.gz 2>/dev/null | tail -n +6 | xargs rm -f
done
