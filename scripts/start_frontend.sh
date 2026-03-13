#!/bin/bash
# Copy Perp 프론트엔드 자동 재시작 스크립트
# - next dev 실행 + 자동 재시작
# - 로그: /tmp/copy-perp-frontend.log

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
FRONTEND_DIR="${FRONTEND_DIR:-/root/.openclaw/workspace/paperclip-company/projects/pacifica-hackathon/copy-perp-web}"
LOG_FILE="/tmp/copy-perp-frontend.log"
PID_FILE="/tmp/copy-perp-frontend.pid"
MAX_RETRIES=5
HEALTH_INTERVAL=10  # seconds
PORT="${FRONTEND_PORT:-3000}"

echo "[$(date)] 🚀 Copy Perp Frontend 시작 (PORT=${PORT})" | tee -a "$LOG_FILE"
echo "[$(date)] 📁 FRONTEND_DIR=${FRONTEND_DIR}" | tee -a "$LOG_FILE"

if [ ! -d "$FRONTEND_DIR" ]; then
    echo "[$(date)] ❌ 프론트엔드 디렉토리 없음: $FRONTEND_DIR" | tee -a "$LOG_FILE"
    exit 1
fi

cd "$FRONTEND_DIR"

# .env.local 생성 (백엔드 URL 설정)
BACKEND_URL="${BACKEND_URL:-http://localhost:8001}"
if [ ! -f ".env.local" ]; then
    echo "NEXT_PUBLIC_API_URL=${BACKEND_URL}" > .env.local
    echo "[$(date)] 📝 .env.local 생성: NEXT_PUBLIC_API_URL=${BACKEND_URL}" | tee -a "$LOG_FILE"
fi

# node_modules 확인
if [ ! -d "node_modules" ]; then
    echo "[$(date)] 📦 npm install 실행..." | tee -a "$LOG_FILE"
    npm install >> "$LOG_FILE" 2>&1
fi

start_frontend() {
    echo "[$(date)] ▶ next dev 시작 (attempt $1/${MAX_RETRIES}, PORT=${PORT})" | tee -a "$LOG_FILE"
    PORT="$PORT" npm run dev >> "$LOG_FILE" 2>&1 &
    FRONTEND_PID=$!
    echo "$FRONTEND_PID" > "$PID_FILE"
    echo "[$(date)] ✅ PID=$FRONTEND_PID" | tee -a "$LOG_FILE"
}

health_check() {
    python3 -c "
import socket, sys
try:
    s = socket.create_connection(('127.0.0.1', ${PORT}), timeout=3)
    s.sendall(b'GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
    data = s.recv(512)
    s.close()
    sys.exit(0 if b'200' in data or b'301' in data or b'302' in data else 1)
except Exception:
    sys.exit(1)
" 2>/dev/null
}

# cleanup on exit
cleanup() {
    echo "[$(date)] 🛑 종료 시그널 수신" | tee -a "$LOG_FILE"
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        kill "$PID" 2>/dev/null || true
        rm -f "$PID_FILE"
    fi
    exit 0
}
trap cleanup SIGTERM SIGINT

# 메인 루프
retry=0
start_frontend 1

# next dev 초기 빌드 대기 (최대 30초)
echo "[$(date)] ⏳ 초기 빌드 대기 (최대 30초)..." | tee -a "$LOG_FILE"
sleep 10

while true; do
    sleep "$HEALTH_INTERVAL"

    if [ ! -f "$PID_FILE" ]; then
        echo "[$(date)] ⚠ PID 파일 없음 — 재시작" | tee -a "$LOG_FILE"
    else
        PID=$(cat "$PID_FILE")
        if ! kill -0 "$PID" 2>/dev/null; then
            echo "[$(date)] ❌ 프로세스 종료 감지 (PID=$PID)" | tee -a "$LOG_FILE"
        elif health_check; then
            retry=0  # 정상
            continue
        else
            # next dev는 초기 빌드 시간 필요 — 즉시 종료하지 않음
            echo "[$(date)] ⚠ Health check 실패 (빌드 중일 수 있음)" | tee -a "$LOG_FILE"
            continue
        fi
    fi

    retry=$((retry + 1))
    if [ "$retry" -gt "$MAX_RETRIES" ]; then
        echo "[$(date)] 💀 최대 재시도 횟수(${MAX_RETRIES}) 초과 — 종료" | tee -a "$LOG_FILE"
        exit 1
    fi

    echo "[$(date)] 🔄 재시작 시도 ${retry}/${MAX_RETRIES}..." | tee -a "$LOG_FILE"
    sleep 3
    start_frontend "$retry"
done
