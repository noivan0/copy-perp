#!/bin/bash
# 연속 페이퍼트레이딩 + Auto-Research 루프
# 사용법: bash scripts/run_continuous_research.sh

set -e
BASE=/root/.openclaw/workspace/paperclip-company/projects/pacifica-hackathon/copy-perp
cd $BASE

echo "======================================"
echo " Continuous Research Pipeline 시작"
echo " $(date '+%Y-%m-%d %H:%M:%S')"
echo "======================================"

# 1라운드: 60분 페이퍼트레이딩
echo ""
echo "[Round 1] 페이퍼트레이딩 60분..."
python3 papertrading/run_papertrading.py --duration 60 --interval 120 2>&1 | tail -20

# Auto-Research 1차
echo ""
echo "[Auto-Research 1차] 파라미터 최적화..."
python3 core/auto_research.py 500 2>&1 | grep -E "✅|초기|최종|수렴|Before|After|→"

# 2라운드: 최적화된 파라미터로 120분 재실행
echo ""
echo "[Round 2] 최적화 파라미터로 120분 재실행..."
python3 papertrading/run_papertrading.py --duration 120 --interval 120 2>&1 | tail -20

# Auto-Research 2차 (더 많은 데이터로)
echo ""
echo "[Auto-Research 2차] 정밀 최적화..."
python3 core/auto_research.py 800 2>&1 | grep -E "✅|초기|최종|수렴|Before|After|→"

echo ""
echo "======================================"
echo " Pipeline 완료: $(date '+%Y-%m-%d %H:%M:%S')"
echo "======================================"
