#!/usr/bin/env bash
# Offsite-забор бэкапов БД price-check с сервера Andrew на локал.
# Запускать по cron (раз в сутки, после ночного серверного бэкапа в 03:00).
# Тянет:
#   - именованные дейли-копии backups/price_check.db.*.bak
#   - свежий price_check.db.bak (берётся перед каждым рестартом бота)
# Локально храним 14 копий (дольше серверных 7 — для страховки).
set -euo pipefail

REMOTE="andrew-server"
REMOTE_DIR="/mnt/backup/price-check"
LOCAL_DIR="/root/price-check/backups"
KEEP=14

mkdir -p "$LOCAL_DIR"

echo "$(date '+%Y-%m-%d %H:%M:%S') pull start" >> "$LOCAL_DIR/pull.log"

# 1. дейли-копии
rsync -az --delete \
  "$REMOTE:$REMOTE_DIR/backups/" "$LOCAL_DIR/" \
  >> "$LOCAL_DIR/pull.log" 2>&1

# 2. самая свежая копия перед рестартом (отдельно, чтобы не потерять между дейли)
rsync -az \
  "$REMOTE:$REMOTE_DIR/price_check.db.bak" \
  "$LOCAL_DIR/price_check.db.latest.bak" \
  >> "$LOCAL_DIR/pull.log" 2>&1

# локальная ротация: оставляем последние KEEP именованных копий
ls -1t "$LOCAL_DIR"/price_check.db.*.bak 2>/dev/null \
  | tail -n +$((KEEP + 1)) | xargs -r rm -f

echo "$(date '+%Y-%m-%d %H:%M:%S') pull done, kept $KEEP (+ latest)" >> "$LOCAL_DIR/pull.log"
