# Copy Perp — Decentralized Copy Trading on Pacifica

> **Pacifica Hackathon 2026 | Track 3: Social & Gamification**

[![Testnet Live](https://img.shields.io/badge/Testnet-LIVE%20%E2%9C%85-brightgreen)](https://test-app.pacifica.fi)
[![Orders Confirmed](https://img.shields.io/badge/Live%20Orders-Confirmed-blue)](https://test-app.pacifica.fi)
[![Traders Monitored](https://img.shields.io/badge/Traders%20Monitored-109-purple)](https://github.com/noivan0/copy-perp)
[![Builder Code](https://img.shields.io/badge/Builder%20Code-noivan-orange)](https://pacifica.gitbook.io/docs/builder-program)

Copy the best perpetual traders on Pacifica — automatically, on-chain, with your funds staying in your wallet.

---

## 🎯 The Problem

CEX copy trading (eToro, Bybit) has millions of users. But:
- **Your funds sit in the exchange** — custody risk, hack risk
- **Bad traders face zero consequences** — no accountability structure
- **Fee splits are opaque** — you don't know what you're actually paying

Perpetual DEXs have the trading. They don't have the copy trading.

**Copy Perp fills that gap — on Pacifica.**

---

## ✨ What Makes It Different

| | CEX Copy Trading | Copy Perp |
|---|---|---|
| Asset custody | Exchange wallet | **Your wallet. Always.** |
| Trader accountability | None | **Performance Bond (collateral)** |
| Fee transparency | Black box | **On-chain via Builder Code** |
| Access | KYC required | **Google login → trade in 30 sec** |
| Market type | Spot/Futures | **Perpetuals on Pacifica** |

---

## 🏗 Architecture

```
┌─────────────────────────────────────────────────────┐
│                  Copy Perp System                   │
│                                                     │
│  Trader opens position on Pacifica                  │
│           │                                         │
│           ▼                                         │
│  ┌─────────────────────┐                            │
│  │   Position Monitor  │  REST 500ms polling        │
│  │  (109 traders live) │  + WS account_positions    │
│  └──────────┬──────────┘                            │
│             │ position change event                 │
│             ▼                                       │
│  ┌─────────────────────┐                            │
│  │    Copy Engine      │  ratio calc + Tier A weight│
│  │  (8 active monitors)│  DataCollector mark price  │
│  └──────────┬──────────┘                            │
│             │ market_order()                        │
│             ▼                                       │
│  ┌─────────────────────┐   CloudFront SNI Bypass    │
│  │  Pacifica REST API  │◄─────────────────────────  │
│  │  (CF SNI spoofing)  │  test-api.pacifica.fi      │
│  └──────────┬──────────┘                            │
│             │ builder_code=noivan                   │
│             ▼                                       │
│  ┌─────────────────────┐                            │
│  │   Builder Code Fee  │  0.1% on follower volume   │
│  │   Fuul Referral     │  viral growth loop         │
│  └─────────────────────┘                            │
└─────────────────────────────────────────────────────┘
```

**Stack:**
- **Backend:** Python + FastAPI + aiosqlite
- **Blockchain:** Pacifica Testnet (Solana, Ed25519 signing)
- **Real-time:** REST 30s polling (68 symbols, DataCollector)
- **Auth/Wallet:** Privy (Google/Twitter social login)
- **Referral:** Fuul SDK
- **Fee capture:** Pacifica Builder Code (`noivan`)
- **HMG Bypass:** CloudFront SNI spoofing (`do5jt23sqak4.cloudfront.net`)

---

## ✅ Live Testnet Evidence

```
Order ID: 296419238 — BTC Long 0.001  → FILLED ✅
Order ID: 296419643 — BTC Short 0.001 → FILLED ✅

Account:  3AHZqrocSguMuo9sUUP8G8YN8NwHwWV2DPUQvbDvtfaQ
Network:  Pacifica Testnet (test-api.pacifica.fi)
Date:     2026-03-13
```

---

## 🚀 Quick Start

```bash
# 1. Clone & install
git clone https://github.com/noivan0/copy-perp
cd copy-perp
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env:
#   AGENT_PRIVATE_KEY=<your_agent_key>
#   ACCOUNT_ADDRESS=<your_account>
#   AGENT_WALLET=<agent_pubkey>
#   BUILDER_CODE=noivan
#   BUILDER_FEE_RATE=0.001

# 3. Run
uvicorn api.main:app --host 0.0.0.0 --port 8001

# 4. Demo Rehearsal (colored terminal output)
python3 scripts/demo_run.py --mock      # Mock mode
python3 scripts/demo_run.py --live      # Real testnet orders
```

---

## 🎬 Demo Terminal Output

```
╔══════════════════════════════════════════════════════╗
║          Copy Perp — LIVE DEMO  (Pacifica testnet)  ║
║              Pacifica Hackathon 2026                 ║
╚══════════════════════════════════════════════════════╝

[12:54:11] [Health] 서버 정상  BTC $72,639  심볼 68개  모니터 8개
[12:54:11] [Stats]  트레이더 109명  팔로워 12명  누적 12건  거래량 $6,100

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  🔔 포지션 감지!
     심볼  : BTC
     방향  : ▲ LONG
     변화량: 0.0500
     가격  : $72,639.03
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[CopyEngine] 팔로워 12명 대상 주문 계산 중...
  ▲ [9mxJJAQw...] BTC LONG 0.000688 @ $72,639  → FILLED ✅  (522ms)
  ▲ [Follower_B] BTC LONG 0.001377 @ $72,639  → FILLED ✅  (453ms)

📊 체결 요약
  총 주문: 6건 | 체결 성공: 6건 | 거래량: $450 | Builder Fee: +$0.45
```

---

## 📡 API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | 서버 상태 + BTC 실시간가 + 모니터 수 |
| GET | `/stats` | 플랫폼 통계 (트레이더/팔로워/거래량) |
| GET | `/markets` | 실시간 가격 (68 심볼, funding, OI) |
| GET | `/traders` | 트레이더 리더보드 (복합 점수 정렬) |
| POST | `/traders` | 트레이더 등록 + 모니터링 시작 |
| GET | `/traders/{addr}/trades` | 트레이더 체결 이력 |
| POST | `/followers/onboard` | 팔로워 온보딩 (Builder Code 서명 포함) |
| GET | `/followers/list` | 활성 팔로워 목록 |
| GET | `/trades` | Copy Trade 내역 (follower/trader/status 필터) |
| GET | `/referral/{address}` | 레퍼럴 링크 + Fuul 포인트 |

---

## 🔑 Key Features

### 1. Real-time Position Mirroring
- 트레이더 포지션 변화 → 팔로워 자동 비례 주문 (500ms 이내)
- 109명 트레이더 동시 모니터링
- 68개 Pacifica 심볼 전체 지원

### 2. Algorithm-Driven Trader Selection
- 복합 점수: 30일 ROI × 0.6 + 7일 ROI × 0.3 + 1일 보너스
- Win Rate, Profit Factor, Max Drawdown, Calmar Ratio 분석
- 219명 중 상위 알고리즘 선별 → 무작위 대비 +5%p ROI

### 3. Tier A Weighted Copying
- 상위 5명 트레이더에 차등 가중치 (0.15~0.30)
- `copy_ratio × tier_weight`로 정확한 비례 실행

### 4. Builder Code Revenue
- 모든 복사 주문에 `builder_code: noivan` 자동 포함
- 팔로워 거래량의 0.1% 플랫폼 수익 자동 적립

### 5. Privy Social Wallet
- Google 로그인 → 지갑 자동 생성
- 30초 만에 카피트레이딩 시작

### 6. Fuul Referral System
- 트레이더 공유 링크 → 팔로워 유입
- 바이럴 성장 루프

---

## 🧪 Backtesting Results

```
분석 기간: 30일  |  슬리피지: 0.1%  |  초기 자본: $10,000

시나리오 1 (copy_ratio=10%) : +41.3% → $14,130
시나리오 2 (copy_ratio=20%) : +82.7% → $18,270  ← 최적
시나리오 3 (copy_ratio=50%) : 리스크 증가

최고 기여 트레이더:
  EYhhf8u9 — WR 14%  PF 162x  기여도 826.9%
  FuHMGqdr — WR 88%  PF 136x  포트폴리오 안정축
  4UBH19qU — WR 100%         리스크 관리 최적
```

---

## 📊 Platform Stats (Live)

| 항목 | 수치 |
|------|------|
| 모니터링 중인 트레이더 | **109명** |
| 활성 팔로워 | **12명** |
| 누적 Copy Trade | **15건** |
| 총 거래량 | **$6,100 USDC** |
| 실시간 심볼 | **68개** |
| 활성 모니터 | **8개** |

---

## 🧪 Test Suite

```bash
python3 -m pytest tests/ -q

# 24/24 PASSED
# ✅ TC-001: basic copy (2 followers)
# ✅ TC-002: side mapping
# ✅ TC-003: liquidation skip
# ✅ TC-004: ratio + tier weight calculation
# ✅ TC-005: multi-symbol (BTC/ETH/SOL)
# ✅ TC-006: no followers — safe exit
# ✅ TC-007: duplicate events — unique IDs
# ✅ TC-008: DB field integrity
# ✅ TC-009: 10 followers concurrent — 3ms
# ✅ TC-010~024: stats, E2E mock, follower onboard
```

---

## 👥 Team

Built by **Pipe Company** for Pacifica Hackathon 2026.

| Role | Responsibility |
|------|---------------|
| 🎯 Strategy | Demo narrative, OKR, positioning |
| 🔍 Research | Trader analysis, API spec, backtesting |
| 💻 Dev | Core engine, FastAPI, Pacifica SDK |
| 🧪 QA | E2E testing, stress test, sign-off |
| 📣 Marketing | Growth, referral, Builder Program |

---

*Testnet only. Not financial advice.*

---

## 🚀 Quick Start

### Testnet (개발/테스트)

```bash
# 환경 설정
cp .env.testnet .env
# .env에서 실제 키 값 입력:
#   ACCOUNT_ADDRESS=<Solana 지갑 주소>
#   AGENT_PRIVATE_KEY=<Base58 개인키>
#   AGENT_WALLET=<에이전트 공개키>

# 서버 시작
python3 -m uvicorn api.main:app --host 0.0.0.0 --port 8001 --reload

# 상태 확인
curl http://localhost:8001/health
curl http://localhost:8001/health/detailed
```

### Mainnet (실서비스)

```bash
cp .env.mainnet .env
# .env에서 MAINNET 키 값 입력 + render.com 배포 후 사용
# GET: codetabs CORS 프록시 자동 사용
# POST: 별도 프록시 서버 필요 (proxy/ 폴더 참조)

NETWORK=mainnet python3 -m uvicorn api.main:app --host 0.0.0.0 --port 8001
```

### 자동 재시작 (프로덕션)

```bash
# systemd 사용
sudo cp copy-perp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable copy-perp
sudo systemctl start copy-perp
sudo journalctl -f -u copy-perp

# supervisor 사용
pip install supervisor
supervisord -c supervisord.conf
```

---

## ⚙️ 환경변수 전체 목록

| 변수 | 설명 | 기본값 |
|------|------|--------|
| `NETWORK` | `testnet` 또는 `mainnet` | `testnet` |
| `TESTNET_REST_URL` | Testnet REST 엔드포인트 | `https://test-api.pacifica.fi/api/v1` |
| `TESTNET_CF_URL` | HMG 우회용 CloudFront SNI | `https://do5jt23sqak4.cloudfront.net` |
| `MAINNET_REST_URL` | Mainnet REST 엔드포인트 | `https://api.pacifica.fi/api/v1` |
| `ACCOUNT_ADDRESS` | Pacifica 계정 주소 | — |
| `AGENT_WALLET` | Agent Key 공개키 | — |
| `AGENT_PRIVATE_KEY` | Agent Key Base58 개인키 | — |
| `BUILDER_CODE` | Builder Program 코드 | `noivan` |
| `BUILDER_FEE_RATE` | 수수료율 | `0.001` |
| `COPY_RATIO` | 복사 비율 (0.0~1.0) | `0.10` |
| `MAX_POSITION_USDC` | 팔로워당 최대 포지션 | `50` |
| `PRIVY_APP_ID` | Privy 로그인 App ID | `""` (데모 모드) |
| `FUUL_API_KEY` | Fuul 레퍼럴 API 키 | `""` (Mock 모드) |
| `FUUL_PROJECT_ID` | Fuul 프로젝트 ID | — |
| `ALERT_TELEGRAM_TOKEN` | 텔레그램 봇 토큰 (알림) | — |
| `ALERT_TELEGRAM_CHAT_ID` | 텔레그램 채팅 ID | — |
| `DB_PATH` | SQLite DB 절대 경로 | `copy_perp.db` |

---

## 📡 API 엔드포인트

| 엔드포인트 | 설명 |
|-----------|------|
| `GET /docs` | Swagger UI (자동 생성) |
| `GET /health` | 서버 상태 (간략) |
| `GET /health/detailed` | 상세 헬스 (모니터 폴링 시각, DB 크기, uptime) |
| `GET /stats` | 플랫폼 통계 |
| `GET /traders` | 트레이더 목록 |
| `POST /traders` | 트레이더 등록 |
| `GET /leaderboard` | 복합 스코어 리더보드 |
| `POST /followers/onboard` | 팔로워 온보딩 (Privy 통합) |
| `GET /followers` | 팔로워 목록 |
| `GET /copy-trades` | 복사 거래 내역 (PnL 포함) |
| `GET /metrics` | Prometheus 형식 메트릭 |
