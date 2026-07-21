#!/usr/bin/env bash
# Деплой price-check бота на сервер Andrew.
# Запускать ИЗ ЛОКАЛЬНОЙ папки /root/price-check (где есть ssh-config с хостом andrew-server).
# Код + .env синхронизируются, НО база данных (*.db) НЕ затирается —
# она исключена из rsync, чтобы не потерять рабочие товары юзеров.
set -euo pipefail

# переходим в папку скрипта, чтобы ./ указывал на проект (не на текущий PWD)
cd "$(dirname "$0")"

REMOTE="andrew-server"
DEST="/mnt/backup/price-check/"

rsync -az --ignore-times --delete \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '*.db' \
  --exclude '*.db-*' \
  --exclude '*.bak' \
  --exclude '.env' \
  --exclude 'backup.log' \
  --exclude 'bot.log' \
  --exclude 'downloaded_files' \
  --exclude '_*.py' \
  ./ "$REMOTE:$DEST"

echo "Deployed to $REMOTE:$DEST (DB preserved)"
