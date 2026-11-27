cat << 'EOF' > ~/Documents/code/dies-natalis-booth/start_booth.sh
#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

if [ -f ".venv/bin/activate" ]; then
    source ".venv/bin/activate"
elif [ -f "venv/bin/activate" ]; then
    source "venv/bin/activate"
fi

uvicorn app:app --host 0.0.0.0 --port 8000 &
SERVER_PID=$!

cleanup() {
    kill "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

for _ in $(seq 1 20); do
    if bash -c "echo > /dev/tcp/127.0.0.1/8000" 2>/dev/null; then
        break
    fi
    sleep 0.5
done

URL="http://localhost:8000"

if command -v firefox >/dev/null 2>&1; then
    firefox --kiosk "$URL"
elif command -v google-chrome >/dev/null 2>&1; then
    google-chrome --kiosk "$URL" --no-first-run --user-data-dir="/tmp/photobooth-kiosk-profile"
elif command -v chromium-browser >/dev/null 2>&1; then
    chromium-browser --kiosk "$URL" --no-first-run --user-data-dir="/tmp/photobooth-kiosk-profile"
else
    xdg-open "$URL"
    wait "$SERVER_PID"
fi
EOF

chmod +x ~/Documents/code/dies-natalis-booth/start_booth.sh