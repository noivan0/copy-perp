#!/usr/bin/env bash
# scripts/run_all_strategies.sh
# 4개 전략 병렬 페이퍼트레이딩 실행기
# 사용법: bash scripts/run_all_strategies.sh [--duration 120] [--interval 120]
#
# 각 전략을 별도 프로세스로 실행, 로그와 결과를 개별 파일로 저장
# mainnet 실데이터 기반 4-전략 비교 데이터 축적

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
PT_DIR="$ROOT_DIR/papertrading"
LOG_DIR="/tmp/copy_perp_pt"
SESSION_DIR="$PT_DIR/sessions"

mkdir -p "$LOG_DIR" "$SESSION_DIR"

DURATION=${DURATION:-120}    # 기본 2시간 세션
INTERVAL=${INTERVAL:-120}    # 기본 2분 폴링

TS=$(date +%Y%m%d_%H%M%S)

echo "=========================================="
echo " Copy Perp 4-전략 병렬 페이퍼트레이딩"
echo " 시작: $(date '+%Y-%m-%d %H:%M:%S')"
echo " 세션 길이: ${DURATION}분 | 폴링: ${INTERVAL}초"
echo "=========================================="

# 기존 실행 중인 페이퍼트레이딩 종료
pkill -f "run_papertrading.py" 2>/dev/null || true
pkill -f "run_longterm.py" 2>/dev/null || true
sleep 2

# 4개 전략 병렬 실행
STRATEGIES=("default" "conservative" "balanced" "aggressive")

for STRAT in "${STRATEGIES[@]}"; do
    OUT="$SESSION_DIR/${STRAT}_${TS}.json"
    LOG="$LOG_DIR/${STRAT}.log"

    echo "[START] $STRAT → log: $LOG"
    cd "$ROOT_DIR"
    python3 papertrading/run_papertrading.py \
        --strategy "$STRAT" \
        --duration "$DURATION" \
        --interval "$INTERVAL" \
        --output   "$OUT" \
        > "$LOG" 2>&1 &

    echo "$!" > "$LOG_DIR/${STRAT}.pid"
    sleep 1  # 동시 API 호출 분산
done

echo ""
echo "4개 전략 모두 시작됨:"
for STRAT in "${STRATEGIES[@]}"; do
    PID=$(cat "$LOG_DIR/${STRAT}.pid" 2>/dev/null || echo "?")
    echo "  $STRAT  PID=$PID  log=$LOG_DIR/${STRAT}.log"
done
echo ""
echo "실시간 대시보드: python3 scripts/pt_dashboard.py"
echo "종료: bash scripts/stop_all_strategies.sh"
