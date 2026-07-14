#!/usr/bin/env bash
# Бэкап БД price-check перед (ре)стартом сервиса.
# Сохраняет копию price_check.db в price_check.db.bak (с датой в имени при необходимости).
set -euo pipefail

DIR="/mnt/backup/price-check"
DB="$DIR/price_check.db"
BAK="$DIR/price_check.db.bak"

if [ -f "$DB" ]; then
    cp -f "$DB" "$BAK"
    echo "$(date '+%Y-%m-%d %H:%M:%S') DB backed up -> $BAK" >> "$DIR/backup.log"
fi
