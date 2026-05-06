#!/bin/bash
# ──────────────────────────────────────────────────────
#  NexOlsen Admin — Lokal starter
#  Dobbeltklik på denne fil for at starte admin-panelet
# ──────────────────────────────────────────────────────

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# Tjek at .env findes
if [ ! -f ".env" ]; then
  osascript -e 'display alert "Mangler .env fil" message "Opret filen .env i klaiai-mappen og udfyld dine API-nøgler.\n\nSe .env filen for vejledning." as critical'
  exit 1
fi

# Indlæs miljøvariabler fra .env
set -a
source .env
set +a

# Tjek Python
if ! command -v python3 &>/dev/null; then
  osascript -e 'display alert "Python ikke fundet" message "Installer Python 3 fra python.org" as critical'
  exit 1
fi

# Opret virtuel miljø hvis det ikke findes
if [ ! -d "venv" ]; then
  echo "📦 Opretter virtuelt Python-miljø..."
  python3 -m venv venv
fi

# Aktiver venv
source venv/bin/activate

# Installer/opdater pakker
echo "📦 Tjekker Python-pakker..."
pip install -q -r requirements.txt

# Start Flask-server i baggrunden
echo "🚀 Starter NexOlsen server på port ${PORT:-5001}..."
python3 -m flask --app agents.app run --port "${PORT:-5001}" &
SERVER_PID=$!

# Vent til serveren er klar
echo "⏳ Venter på server..."
for i in {1..20}; do
  if curl -s "http://localhost:${PORT:-5001}/health" &>/dev/null || \
     curl -s "http://localhost:${PORT:-5001}/app/admin.html" &>/dev/null; then
    break
  fi
  sleep 0.5
done

# Åbn browser
echo "🌐 Åbner admin-panel..."
open "http://localhost:${PORT:-5001}/app/admin.html"

echo ""
echo "✅ NexOlsen Admin kører på http://localhost:${PORT:-5001}/app/admin.html"
echo "   Tryk Ctrl+C eller luk dette vindue for at stoppe serveren."
echo ""

# Hold terminalen åben og vent på Ctrl+C
trap "echo ''; echo '🛑 Stopper server...'; kill $SERVER_PID 2>/dev/null; exit 0" INT TERM
wait $SERVER_PID
