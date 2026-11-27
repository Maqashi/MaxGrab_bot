#!/usr/bin/env bash
set -e

echo "=== 1/3 Installing Docker & Git ==="
sudo apt-get update -y
sudo apt-get install -y ca-certificates curl git docker.io docker-compose-v2
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER" || true

echo "=== 2/3 Checking .env and cookies.txt ==="
if [ ! -f .env ]; then
  cat <<'EOF' > .env
BOT_TOKEN=PASTE_BOT_TOKEN
API_ID=PASTE_API_ID
API_HASH=PASTE_API_HASH
EOF
  echo "Создан шаблон .env — впиши туда BOT_TOKEN, API_ID и API_HASH командой: nano .env"
fi

if [ ! -f cookies.txt ]; then
  touch cookies.txt
  echo "Создан пустой cookies.txt — вставь туда YouTube cookies командой: nano cookies.txt"
fi

mkdir -p data

echo "=== 3/3 Building & starting MaxGrabBot 24/7 ==="
sudo docker compose up -d --build
echo "Готово! Логи смотреть командой: sudo docker compose logs -f"
