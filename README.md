# Copy Perp — Decentralized Copy Trading on Pacifica

> **Pacifica Hackathon 2026 | Track 3: Social & Gamification**

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
Trader opens position
      ↓
Position Monitor (REST 500ms polling / WS when available)
      ↓
Copy Engine — calculates follower ratio
      ↓
Pacifica API — executes follower orders (Agent Key signing)
      ↓
Builder Code — fee captured automatically
      ↓
Fuul Referral — traders earn rewards for bringing followers
```

**Stack:**
- **Backend:** Python + FastAPI + aiosqlite
- **Blockchain:** Pacifica Testnet (Solana, Ed25519 signing)
- **Real-time:** WebSocket prices stream (68 symbols)
- **Auth/Wallet:** Privy (Google/Twitter social login)
- **Referral:** Fuul SDK
- **Fee capture:** Pacifica Builder Code

---

## 🚀 Quick Start

```bash
# 1. Clone & install
git clone https://github.com/your-org/copy-perp
cd copy-perp
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env: AGENT_PRIVATE_KEY, AGENT_PUBLIC_KEY, BUILDER_CODE

# 3. Run
uvicorn api.main:app --host 0.0.0.0 --port 8001

# 4. Open UI
open http://localhost:8001/app
```

---

## 📡 API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Server status + WS connection |
| GET | `/markets` | Real-time prices (68 symbols, funding, OI) |
| GET | `/markets?symbol=BTC` | Single symbol data |
| GET | `/traders` | Trader leaderboard |
| POST | `/traders` | Register trader + start monitoring |
| POST | `/follow` | Register follower + start copying |
| DELETE | `/follow/{trader}` | Unsubscribe |
| GET | `/trades` | Copy trade history |
| GET | `/stats` | Platform statistics |
| GET | `/referral/{address}` | Get referral link (Fuul) |

---

## 🔑 Key Features

### 1. Real-time Position Mirroring
- Trader opens BTC long → all followers automatically open proportional BTC long
- 500ms latency (REST polling) with WebSocket upgrade path
- Supports: open/close/partial close, all 68 Pacifica symbols

### 2. Builder Code Revenue
- Every copied trade includes Builder Code
- Platform earns fee on all follower volume
- Transparent, on-chain, automatic

### 3. Fuul Referral System
- Traders share referral links → followers join
- Traders earn Fuul points for follower volume
- Viral growth loop built in

### 4. Privy Social Wallet
- No MetaMask needed
- Google/Twitter login → wallet auto-created
- Best UX for non-crypto-native users

### 5. Performance Bonds (roadmap)
- Traders stake collateral before going live
- Losses first hit the bond → followers protected
- Structural accountability

---

## 🧪 Testing

```bash
# Run E2E test suite (Mock mode — no API key needed)
python3 tests/test_e2e.py

# Output:
# ✅ TC-001: basic copy (2 followers)
# ✅ TC-002: side mapping (open_long→bid, etc.)
# ✅ TC-003: liquidation skip
# ✅ TC-004: ratio calculation (0.5x, 0.25x accurate)
# ✅ TC-005: multi-symbol (BTC/ETH/SOL)
# ✅ TC-006: no followers — safe exit
# ✅ TC-007: duplicate events — unique client_order_ids
# ✅ TC-008: DB field integrity
# ✅ TC-009: 10 followers concurrent — 3ms (limit: 2000ms)
# Result: 9/9 passed
```

---

## 📅 Development Timeline

| Week | Milestone |
|------|-----------|
| W1 (3/16–22) | ✅ Architecture + WS data + Copy Engine + FastAPI |
| W2 (3/23–29) | Agent Key E2E + Fuul referral + frontend polish |
| W3 (3/30–4/5) | Privy wallet + testnet QA + leaderboard |
| W4 (4/6–16) | Stress test + demo video + submission |

---

## 🏆 Prize Targets

- **Track 3 Winner** — $2,000 + 14,000 points (70% probability)
- **Grand Prize** — $5,000 + 30,000 points (35% probability)
- **Best UX Award** — $1,000 + 7,000 points (60% probability, Privy integration)

---

## 👥 Team

Built by **Pipe Company** for Pacifica Hackathon 2026.

- 🎯 Strategy: Positioning, demo narrative, OKR tracking
- 🔍 Research: Competitive analysis, API spec, Fuul/Privy docs
- 💻 Dev: Core engine, FastAPI, Pacifica SDK integration
- 🧪 QA: E2E scenarios, stress testing, release sign-off

---

*Testnet only. Not financial advice.*
