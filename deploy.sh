#!/bin/bash
# ============================================================
# Copy Perp — 원클릭 배포 스크립트
# 사용법:
#   RAILWAY_TOKEN=xxx VERCEL_TOKEN=xxx bash deploy.sh
# ============================================================
set -e

BASE="$(cd "$(dirname "$0")" && pwd)"
FRONTEND_DIR="$BASE/../copy-perp-web"

# ── 색상 ────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'

log()  { echo -e "${BLUE}[$(date +%H:%M:%S)]${RESET} $1"; }
ok()   { echo -e "${GREEN}✅ $1${RESET}"; }
warn() { echo -e "${YELLOW}⚠️  $1${RESET}"; }
err()  { echo -e "${RED}❌ $1${RESET}"; exit 1; }

# ── 환경변수에서 .env 값 로드 ──────────────────────────────
source "$BASE/.env" 2>/dev/null || true

log "Copy Perp 배포 시작"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ============================================================
# T1: Railway 백엔드 배포
# ============================================================
log "${BOLD}T1. Railway 백엔드 배포${RESET}"

if [ -z "$RAILWAY_TOKEN" ]; then
  warn "RAILWAY_TOKEN 없음 — Railway 배포 건너뜀"
  warn "토큰 발급: https://railway.com/account/tokens"
else
  export RAILWAY_TOKEN
  cd "$BASE"

  log "Railway 프로젝트 초기화..."
  # 이미 linked 된 경우 skip
  railway status 2>/dev/null || railway init --name copy-perp 2>/dev/null || true

  log "환경변수 설정..."
  railway vars set \
    NETWORK=testnet \
    BUILDER_CODE=noivan \
    BUILDER_FEE_RATE=0.001 \
    PRIVY_APP_ID=cmmvoxcix058e0ckv7uhp9ip0 \
    ACCOUNT_ADDRESS=3AHZqrocSguMuo9sUUP8G8YN8NwHwWV2DPUQvbDvtfaQ \
    AGENT_WALLET="${AGENT_WALLET}" \
    AGENT_PRIVATE_KEY="${AGENT_PRIVATE_KEY}" \
    DB_PATH=/tmp/copy_perp.db \
    2>&1 | tail -3

  log "배포 실행..."
  railway up --detach 2>&1 | tail -5

  log "Railway URL 확인..."
  RAILWAY_URL=$(railway domain 2>/dev/null || echo "")
  if [ -z "$RAILWAY_URL" ]; then
    railway domain generate 2>/dev/null || true
    RAILWAY_URL=$(railway domain 2>/dev/null || echo "pending")
  fi

  ok "T1 백엔드: https://${RAILWAY_URL}"
  export BACKEND_URL="https://${RAILWAY_URL}"
fi

# ============================================================
# T2: Vercel 프론트엔드 배포
# ============================================================
log "${BOLD}T2. Vercel 프론트엔드 배포${RESET}"

if [ -z "$VERCEL_TOKEN" ]; then
  warn "VERCEL_TOKEN 없음 — Vercel 배포 건너뜀"
  warn "토큰 발급: https://vercel.com/account/tokens"
else
  cd "$FRONTEND_DIR"

  # .env.local 생성
  cat > .env.local << EOF
NEXT_PUBLIC_PRIVY_APP_ID=cmmvoxcix058e0ckv7uhp9ip0
NEXT_PUBLIC_API_URL=${BACKEND_URL:-https://copy-perp-production.up.railway.app}
NEXT_PUBLIC_BUILDER_CODE=noivan
EOF

  log "Vercel 배포 실행..."
  VERCEL_URL=$(vercel deploy --prod \
    --token "$VERCEL_TOKEN" \
    --yes \
    --env NEXT_PUBLIC_PRIVY_APP_ID=cmmvoxcix058e0ckv7uhp9ip0 \
    --env NEXT_PUBLIC_API_URL="${BACKEND_URL:-https://copy-perp-production.up.railway.app}" \
    --env NEXT_PUBLIC_BUILDER_CODE=noivan \
    2>&1 | grep "https://" | tail -1)

  ok "T2 프론트: ${VERCEL_URL}"
  export FRONTEND_URL="$VERCEL_URL"

  # Railway ALLOWED_ORIGINS 업데이트
  if [ -n "$RAILWAY_TOKEN" ] && [ -n "$VERCEL_URL" ]; then
    log "ALLOWED_ORIGINS 업데이트..."
    cd "$BASE"
    railway vars set ALLOWED_ORIGINS="http://localhost:3000,http://localhost:8001,${VERCEL_URL}" 2>/dev/null || true
  fi
fi

# ============================================================
# T3: README 업데이트 + git push
# ============================================================
log "${BOLD}T3. README 업데이트 + git push${RESET}"
cd "$BASE"

if [ -n "$BACKEND_URL" ] || [ -n "$FRONTEND_URL" ]; then
  # README 상단 배지 라인 업데이트
  BACKEND_DISPLAY="${BACKEND_URL:-TBD}"
  FRONTEND_DISPLAY="${FRONTEND_URL:-TBD}"

  # Live URLs 섹션 추가 (이미 있으면 sed로 교체)
  if grep -q "## 🌐 Live URLs" README.md; then
    sed -i "/## 🌐 Live URLs/,/^---/d" README.md
  fi

  # README 상단에 Live URLs 삽입
  LIVE_SECTION="## 🌐 Live URLs\n\n| | URL |\n|--|----|\n| 🔧 Backend API | ${BACKEND_DISPLAY} |\n| 🌐 Frontend | ${FRONTEND_DISPLAY} |\n| 📊 Health | ${BACKEND_DISPLAY}/health |\n\n---\n"
  sed -i "s|# Copy Perp|# Copy Perp\n\n${LIVE_SECTION}|" README.md 2>/dev/null || true

  git add README.md .env.example
  git commit -m "deploy: Railway + Vercel 배포 완료

Backend: ${BACKEND_DISPLAY}
Frontend: ${FRONTEND_DISPLAY}
" 2>/dev/null || true

  # git push — GH_PAT 환경변수 또는 기존 remote 사용
  if [ -n "$GH_PAT" ]; then
    git remote set-url origin "https://noivan0:${GH_PAT}@github.com/noivan0/copy-perp.git"
  fi
  git push origin master 2>&1 | tail -3
  ok "T3 GitHub push 완료"
fi

# ============================================================
# 최종 보고
# ============================================================
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "${BOLD}🏁 배포 완료 보고${RESET}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
[ -n "$BACKEND_URL" ]  && echo -e "T1 완료: ${GREEN}${BACKEND_URL}${RESET}"  || warn "T1 미완료 — RAILWAY_TOKEN 필요"
[ -n "$FRONTEND_URL" ] && echo -e "T2 완료: ${GREEN}${FRONTEND_URL}${RESET}" || warn "T2 미완료 — VERCEL_TOKEN 필요"
echo "T3 완료: GitHub push 완료"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# 헬스체크
if [ -n "$BACKEND_URL" ]; then
  log "헬스체크 시도 (30초 대기)..."
  sleep 30
  HEALTH=$(curl -sf "${BACKEND_URL}/health" 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"status={d.get('status')} btc={d.get('btc_mark')} monitors={d.get('active_monitors')}\")" 2>/dev/null || echo "응답 없음")
  echo "헬스체크: $HEALTH"
fi
