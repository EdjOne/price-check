#!/usr/bin/env bash
# Ежедневный бэкап БД price-check с ротацией (храним 7 копий).
# Запускать по cron от юзера andrew (или через systemd timer).
set -euo pipefail

DIR="/mnt/backup/price-check"
DB="$DIR/price_check.db"
BAK_DIR="$DIR/backups"
KEEP=7

mkdir -p "$BAK_DIR"

if [ ! -f "$DB" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') DB not found, skip" >> "$DIR/backup.log"
    exit 0
fi

STAMP="$(date '+%Y%m%d_%H%M%S')"
cp -f "$DB" "$BAK_DIR/price_check.db.$STAMP.bak"
echo "$(date '+%Y-%m-%d %H:%M:%S') daily backup -> $BAK_DIR/price_check.db.$STAMP.bak" >> "$DIR/backup.log"

# ротация: оставляем только последние KEEP копий
ls -1t "$BAK_DIR"/price_check.db.*.bak 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
echo "$(date '+%Y-%m-%d %H:%M:%S') rotated, kept $KEEP" >> "$DIR/backup.log"
