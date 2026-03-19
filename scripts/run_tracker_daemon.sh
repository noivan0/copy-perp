#!/bin/bash
# CopyPerp Mainnet 성과 추적 데몬
# 1시간마다 스냅샷 수집 및 분석 실행

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$BASE_DIR"

echo "[$(date)] CopyPerp Mainnet 추적 데몬 시작"
echo "[$(date)] 작업 디렉토리: $BASE_DIR"

while true; do
    echo "[$(date)] ▶ mainnet_tracker.py 실행 중..."
    python3 scripts/mainnet_tracker.py
    
    echo "[$(date)] ▶ analyze_accumulated.py 실행 중..."
    python3 scripts/analyze_accumulated.py
    
    echo "[$(date)] ✅ 사이클 완료. 다음 수집까지 3600초 대기..."
    sleep 3600
done
