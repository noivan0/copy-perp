#!/bin/bash
# Copy Perp 백엔드 자동 재기동 스크립트
# - uvicorn 실행 + 5초마다 health check
# - 다운되면 자동 재기동 (최대 5회 재시도)
# - 로그: /tmp/copy-perp.log

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_FILE="/tmp/copy-perp.log"
PID_FILE="/tmp/copy-perp.pid"
MAX_RETRIES=5
HEALTH_INTERVAL=5  # seconds
PORT="${PORT:-8001}"

# ENV 파일 로드 (NETWORK 기반 자동 선택)
NETWORK="${NETWORK:-testnet}"
if [ -f "$PROJECT_DIR/.env.${NETWORK}" ]; then
    echo "[$(date)] 🔧 Loading .env.${NETWORK}" | tee -a "$LOG_FILE"
    export $(grep -v '^#' "$PROJECT_DIR/.env.${NETWORK}" | xargs)
elif [ -f "$PROJECT_DIR/.env" ]; then
    echo "[$(date)] 🔧 Loading .env (default)" | tee -a "$LOG_FILE"
    export $(grep -v '^#' "$PROJECT_DIR/.env" | xargs)
fi

echo "[$(date)] 🚀 Copy Perp Backend 시작 (NETWORK=${NETWORK}, PORT=${PORT})" | tee -a "$LOG_FILE"

cd "$PROJECT_DIR"

start_server() {
    echo "[$(date)] ▶ uvicorn 시작 (attempt $1/${MAX_RETRIES})" | tee -a "$LOG_FILE"
    uvicorn api.main:app --host 0.0.0.0 --port "$PORT" --log-level info \
        >> "$LOG_FILE" 2>&1 &
    SERVER_PID=$!
    echo "$SERVER_PID" > "$PID_FILE"
    echo "[$(date)] ✅ PID=$SERVER_PID" | tee -a "$LOG_FILE"
}

health_check() {
    # raw socket health check (HMG 필터 우회 방식)
    python3 -c "
import socket, ssl, sys
try:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    s = socket.create_connection(('127.0.0.1', ${PORT}), timeout=3)
    s.sendall(b'GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
    data = s.recv(512)
    s.close()
    sys.exit(0 if b'200' in data or b'ok' in data.lower() else 1)
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
start_server 1

while true; do
    sleep "$HEALTH_INTERVAL"

    if [ ! -f "$PID_FILE" ]; then
        echo "[$(date)] ⚠ PID 파일 없음 — 재기동" | tee -a "$LOG_FILE"
    else
        PID=$(cat "$PID_FILE")
        if ! kill -0 "$PID" 2>/dev/null; then
            echo "[$(date)] ❌ 프로세스 종료 감지 (PID=$PID)" | tee -a "$LOG_FILE"
        elif health_check; then
            retry=0  # 정상 — 재시도 카운터 리셋
            continue
        else
            echo "[$(date)] ⚠ Health check 실패 — 프로세스는 살아있지만 응답 없음" | tee -a "$LOG_FILE"
            kill "$PID" 2>/dev/null || true
        fi
    fi

    retry=$((retry + 1))
    if [ "$retry" -gt "$MAX_RETRIES" ]; then
        echo "[$(date)] 💀 최대 재시도 횟수(${MAX_RETRIES}) 초과 — 종료" | tee -a "$LOG_FILE"
        exit 1
    fi

    echo "[$(date)] 🔄 재기동 시도 ${retry}/${MAX_RETRIES}..." | tee -a "$LOG_FILE"
    sleep 2
    start_server "$retry"
done
