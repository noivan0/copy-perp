# Copy Perp — Demo Script (Final)
> Pacifica Hackathon 2026 | 2026-03-13 업데이트
> E2E 검증 완료 버전 — Privy 로그인 → 리더보드 → 팔로우 → 복사 주문 체결

---

## 🎬 데모 구성 (10분)

| 섹션 | 시간 | 핵심 |
|------|------|------|
| 오프닝 | 1분 | DEX에 카피트레이딩이 없다 |
| 리더보드 + 알고리즘 | 2분 | 208명 중 선별 로직 |
| **Privy 로그인 → 팔로우** | 2분 | ← 신규 핵심 |
| Copy Engine Live | 3분 | 포지션 감지 → FILLED |
| 데이터 증명 | 1분 | 백테스팅 +82.7% |
| 클로징 | 1분 | Builder Code + 로드맵 |

---

## Step 0 — 데모 시작 전 준비

```bash
# 터미널 1: 백엔드 서버
cd copy-perp
uvicorn api.main:app --host 0.0.0.0 --port 8001

# 터미널 2: 데모 스크립트 (핵심 시각화)
python3 scripts/demo_run.py --mock   # 또는 --live

# 브라우저: 프론트엔드
open http://localhost:3000
```

**E2E 검증 결과 (2026-03-13):**
```
[1] 서버: BTC $72,796 | 모니터 8개 | rest_poll ✅
[2] TOP 트레이더: EcX5xSDT... ROI=82.5%  ✅
[3] 온보딩: ok=True | 2명 등록 ✅
[4] 활성 팔로워: 12명 ✅
[5] Copy Trades: 15건 | 거래량 $6,100 ✅
[6] 플랫폼: 트레이더 208명 | 팔로워 12명 ✅
```

---

## Scene 1 — 오프닝 (1분)

> "CEX 카피트레이딩 써보신 분 계세요?
> Bybit, eToro — 수백만 명이 버튼 하나로 고수 따라하는 서비스.
> 근데 퍼프 DEX엔 없습니다. Hyperliquid도, dYdX도, GMX도.
> **Copy Perp이 그 공백을 채웁니다 — Pacifica 위에서.**
> 
> 차이는 하나. 당신 자산이 당신 지갑에 있습니다."

**화면:** 히어로 섹션 — "Copy Top Traders on Pacifica DEX"

---

## Scene 2 — 리더보드 (2분)

**화면:** 웹 리더보드 (localhost:3000)

```
🥇 EcX5xSDT...  30d ROI: +82.5%  승률: 74%  [팔로우]
🥈 4UBH19qU...  30d ROI: +58.4%  승률: 100% [팔로우]
🥉 A6VY4ZBU...  30d ROI: +58.9%  승률: 49%  [팔로우]
⭐ TOP2
✅ TIER1
```

**멘트:**
> "208명 중 5중 필터로 선별했습니다.
> 30일/7일 ROI, 승률, Profit Factor, 최대 낙폭.
> 무작위 팔로우 대비 +5%p ROI 차이가 납니다."

**포인트:** 배지 🏆⭐✅🔵 눈에 보이게, 실시간 BTC 가격 변동

---

## Scene 3 — Privy 로그인 → 팔로우 (2분) ← 신규

**화면:** 리더보드에서 "팔로우" 버튼 클릭

```
[로그인 모달 팝업]

  ┌────────────────────────────────┐
  │     Copy Perp 시작하기         │
  │  소셜 로그인으로 지갑 자동 생성  │
  │                                │
  │  [G] Google로 시작             │
  │  [👻] 지갑 연결 (Phantom)      │
  │                                │
  │  Non-custodial · 30초 설정     │
  └────────────────────────────────┘
```

**멘트:**
> "MetaMask 필요 없습니다. Google 계정만 있으면 됩니다.
> 로그인하면 Privy가 Solana 지갑을 자동 생성하고,
> 바로 Builder Code 서명 + 팔로우가 완료됩니다."

**온보딩 API 흐름:**
```
POST /followers/onboard
  ↓
1. Builder Code 'noivan' 서명 생성
2. Pacifica approve API 호출
3. DB 팔로워 등록 (copy_ratio=10%, max=$50)
4. Tier1 트레이더 2명 자동 팔로우
5. Fuul follow 이벤트 발송
  ↓
{ "ok": true, "registered": ["EcX5xSDT...", "4UBH19qU..."] }
```

---

## Scene 4 — Copy Engine Live (3분) ← 핵심

**화면:** 터미널 2 (demo_run.py 출력)

```
╔══════════════════════════════════════════════════════╗
║          Copy Perp — LIVE DEMO  (Pacifica testnet)  ║
╚══════════════════════════════════════════════════════╝

[12:54:11] [Health] BTC $72,796  심볼 68개  모니터 8개

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  🔔 포지션 감지!
     심볼  : BTC
     방향  : ▲ LONG
     변화량: 0.0500
     가격  : $72,796.00
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[CopyEngine] 팔로워 12명 대상 주문 계산 중...
  ▲ [3AHZqroc...] BTC LONG 0.000688 @ $72,796  → FILLED ✅  (522ms)
  ▲ [Follower_B]  BTC LONG 0.001377 @ $72,796  → FILLED ✅  (453ms)

📊 총 6건 | 체결 6/6 | 거래량 $450 | Builder Fee +$0.45
```

**멘트:**
> "트레이더가 BTC 롱 잡는 순간, 팔로워들한테 비례해서 자동 주문 나갑니다.
> 522ms. 이게 DEX 카피트레이딩의 속도입니다.
> 모든 주문에 Builder Code 'noivan'이 붙어서,
> Pacifica가 거래마다 수수료를 우리 지갑에 자동 적립합니다."

**Live 주문 증거:**
```
Order ID: 296419238 — BTC Long  → FILLED ✅ (실제 체결)
Order ID: 296419643 — BTC Short → FILLED ✅ (실제 체결)
```

---

## Scene 5 — 데이터 증명 (1분)

**30일 백테스팅**

| 전략 | 결과 |
|------|------|
| 무작위 팔로우 | -3.2% |
| Copy Perp 선별 (ratio=10%) | +41.3% |
| Copy Perp 선별 (ratio=20%) | **+82.7%** ← 최적 |

**핵심 트레이더:**
```
EYhhf8u9 — WR 14% / PF 162x  기여도 826.9%  (소수 대형 포지션)
FuHMGqdr — WR 88% / PF 136x  포트폴리오 안정축
4UBH19qU — WR 100%            리스크 최소
```

---

## Scene 6 — 클로징 (1분)

```
지금 이 순간:
✅ Pacifica 테스트넷 LIVE
✅ 실계정 주문 체결 확인 (ID: 296419238)
✅ 208명 트레이더 실시간 모니터링
✅ Privy 로그인 → 팔로우 플로우 완성
✅ Fuul 레퍼럴 이벤트 연동
✅ Builder Code 'noivan' (승인 처리 중)
```

> "Copy Perp은 해커톤 제출물이 아닙니다.
> Builder Program Volume 2 기간 동안 실 팔로워를 유치하면서
> Pacifica에 월 수백만 달러 거래량을 공급하는 인프라입니다."

---

## Q&A 대비

| 예상 질문 | 답변 |
|---------|------|
| Builder Code 승인? | Pacifica 팀 처리 중. 서명 플로우 구현 완료. |
| WS 실시간 아닌가요? | REST 500ms 폴링. 카피트레이딩 목적 충분. |
| 손실 나면? | 팔로워도 손실. 알고리즘 선별 + 자동 손절 예정. |
| 메인넷 언제? | Builder Code 승인 후 즉시 전환 가능 구조. |
| Privy 없이? | Phantom/Solflare 직접 연결도 지원. |

---

## 데모 체크리스트

```
□ uvicorn 포트 8001 기동
□ BTC 실시간가 표시 확인 ($7x,xxx)
□ 리더보드 배지 🏆⭐✅🔵 표시 확인
□ 팔로우 버튼 클릭 → 로그인 모달 팝업 확인
□ demo_run.py --mock 출력 컬러 정상 확인
□ 터미널 폰트 크기 18pt+ (심사위원 가독성)
□ Builder Code 승인 상태 당일 확인
```

---

## 실행 커맨드 요약

```bash
# 서버
cd copy-perp && uvicorn api.main:app --port 8001

# 데모 터미널 (핵심)
python3 scripts/demo_run.py --mock   # 빠른 리허설
python3 scripts/demo_run.py --live   # 실제 주문

# 프론트
cd copy-perp-web && npm run dev

# E2E 검증
python3 - << 'EOF'
import urllib.request, json
print(json.loads(urllib.request.urlopen("http://localhost:8001/health").read()))
EOF
```
