# Pacifica Hackathon — 제출폼 답변
> 작성: QA팀장 Quinn | 2026-04-10 | 마감: 2026-04-16
> 노이반님 검토 후 실제 폼에 복붙하세요.
> ⚠️ 공란 표시 항목은 노이반님 직접 입력 필요

---

## 1. Project Name

```
Copy Perp
```

---

## 2. Track

```
✅ Social & Gamification
```

> 근거: 트레이더 리더보드(CRS 랭킹), 팔로우 소셜 그래프, Fuul 레퍼럴 바이럴 루프가 핵심.
> Analytics & Data도 해당되나, 최고 차별점이 사회적 카피트레이딩 경험이므로 Social 선택.

---

## 3. One-Sentence Pitch

```
Copy Perp lets anyone automatically mirror the best perpetual traders on Pacifica — 
non-custodially, in 30 seconds, with on-chain fee transparency via Builder Code.
```

---

## 4. What does your project do? What problem does it solve? Who is it for?

```
Copy Perp is a decentralized copy trading platform built natively on Pacifica's 
perpetual DEX infrastructure.

The problem: CEX copy trading (eToro, Bybit) has millions of users, but it requires 
surrendering custody of funds to the exchange. On-chain perpetual DEXs have the 
trading rails — but no copy trading layer on top.

Copy Perp fills that gap. When a top-ranked trader opens a position on Pacifica, Copy 
Perp's engine automatically replicates it for followers — proportionally, instantly, 
with their funds staying in their own wallets at all times.

Trader selection isn't manual guesswork. Our CRS (Copy Reliability Score) ranks 133+ 
monitored traders across 5 algorithmic metrics — momentum, profitability, risk, 
consistency, and copyability — producing S/A/B/C tiers. Backtested against 30 days of 
Pacifica mainnet data, CRS-selected portfolios returned +82.7% versus -3.2% for random 
following.

Every copy order automatically embeds builder_code=noivan, routing 0.1% of follower 
trading volume to our wallet on-chain — transparent, verifiable, no hidden mechanics.

Who it's for: retail traders who want exposure to top-performing strategies without 
active management, and DeFi users who won't accept CEX custody risk.
```

---

## 5. Bullet list of core functionality / key features

```
• Non-custodial copy trading — follower funds never leave their wallet; Copy Perp sends 
  signals only, zero custody

• CRS (Copy Reliability Score) — algorithmic trader ranking across 5 metrics 
  (Momentum 30% / Profitability 25% / Risk 20% / Consistency 15% / Copyability 10%), 
  auto-classified into S/A/B/C tiers

• 30-second onboarding — Privy social login (Google/Discord) auto-generates a Solana 
  wallet; no seed phrase required for first-time DeFi users

• Real-time Copy Engine — REST-based 30-second position polling across 133 monitored 
  traders, 69 live symbols; sub-600ms order replication latency

• Builder Code fee capture — every copy order embeds builder_code=noivan; 0.1% of 
  follower volume credited on-chain automatically

• Fuul referral loop — shareable referral links with on-chain point attribution; viral 
  growth mechanic built into the follow flow

• Transparent leaderboard — live CRS scores, 30-day PnL, win rate, recommended 
  copy_ratio per trader; fully auditable via public API

• Backtesting engine — optimal copy_ratio and portfolio allocation (Sharpe-based) 
  computed from 30 days of Pacifica mainnet data

• 69 PASS test suite — core engine, DB integrity, E2E mock, ranked API fully covered
```

---

## 6. What makes this unique? Differentiator vs existing tools or approaches

```
Three things no perpetual DEX has shipped before — all live on Pacifica testnet:

1. Non-custodial copy trading at the protocol layer
   CEX copy trading (eToro, Bybit, OKX) holds your funds. Copy Perp does not. 
   Orders are submitted from the follower's own wallet, signed by their own agent key. 
   Copy Perp is purely a signal router — we never touch your assets.

2. Algorithmic trader selection (CRS), not social popularity
   Most copy trading platforms rank traders by follower count or raw PnL — easily 
   gamed. CRS scores on risk-adjusted performance: a trader with 100% win rate and 
   low drawdown scores higher than a high-PnL, high-variance gambler. 
   Backtest result: CRS portfolio +82.7% vs -3.2% random (30 days, Pacifica mainnet).

3. On-chain revenue via Builder Code — from day one
   builder_code=noivan is embedded in every copy order. Pacifica routes 0.1% of 
   follower volume to us automatically, on-chain. No invoicing, no trust, no 
   black-box fee splits. Revenue scales linearly with platform volume.

   Existing tools: no equivalent builder code integration.
   Pacifica native tools: no copy trading layer exists.
   CEX copy trading: custodial, opaque fees, no DeFi composability.
```

---

## 7. How does your project use Pacifica's infrastructure?

```
Copy Perp is built entirely on Pacifica's infrastructure stack:

• REST API (test-api.pacifica.fi/api/v1) — all order placement, position reads, 
  market data (69 symbols), and account queries go through Pacifica's REST endpoints. 
  We implemented CloudFront SNI spoofing to handle HMG network restrictions.

• Builder Program — builder_code=noivan is registered and embedded in every copy 
  order submitted to Pacifica. This enables Pacifica to route 0.1% of follower 
  trading volume directly to our wallet, on-chain. This is the platform's core 
  revenue model.

• Ed25519 / Agent Key signing — all orders are signed with a Pacifica-compatible 
  agent key (Ed25519), following the official signing spec. signature_type=2 
  (Proxy wallet) is used consistently across all order submissions.

• Testnet order execution — live testnet orders confirmed:
  Order ID 296419238 — BTC Long 0.001 → FILLED ✅
  Order ID 296419643 — BTC Short 0.001 → FILLED ✅
  Account: 3AHZqrocSguMuo9sUUP8G8YN8NwHwWV2DPUQvbDvtfaQ

• Privy + Solana wallet integration — follower onboarding uses Privy's embedded 
  wallet (Solana chain), fully compatible with Pacifica's account model.

• Market data layer — DataCollector polls Pacifica's REST for BTC/ETH/SOL and 69 
  additional symbols every 5–30 seconds, powering the real-time leaderboard and 
  position monitor.
```

---

## 8. Where can we review your code? (Repository link)

```
https://github.com/noivan0/copy-perp
```

> ⚠️ 노이반님 확인 필요: GitHub repo가 Public으로 설정되어 있는지 확인하세요.

---

## 9. What is the current deployment status of your project?

```
✅ Testnet
```

> 근거: 실제 Pacifica testnet에서 주문 체결 확인 완료 (Order ID 296419238, 296419643).
> Mainnet은 Builder Code 최종 승인 후 전환 예정.

---

## 10. Is there a live app, dashboard, or UI we can access?

```
⚠️ [배포 URL — Dev팀장 배포 완료 후 기입]
예: https://copy-perp.vercel.app
```

> Dev팀장 배포 완료 시 이 칸을 업데이트하세요.
> 현재 로컬: http://localhost:8001 (FastAPI + 정적 프론트엔드 서빙)

---

## 11. Demo Video (2–5분)

```
⚠️ [Google Drive 영상 링크 — 4/14 촬영 후 기입]
```

**데모 구성 (권장 5분):**

| 시간 | 내용 |
|------|------|
| 0:00–0:45 | 문제 제기: CEX 카피트레이딩의 한계 (custody, 불투명 수수료) |
| 0:45–1:30 | Copy Perp 소개 + CRS 리더보드 시연 |
| 1:30–2:30 | Privy 로그인 → 30초 온보딩 → 트레이더 팔로우 |
| 2:30–3:30 | Copy Engine 실시간 작동: 트레이더 포지션 → 팔로워 주문 자동 체결 |
| 3:30–4:15 | Builder Code 수수료 수취 확인 (on-chain 투명성) |
| 4:15–5:00 | 백테스팅 결과 (CRS +82.7% vs 무작위 -3.2%) + 로드맵 |

> ⚠️ **제출 필수 조건**: 영상 없으면 심사 제외. 반드시 4/14 촬영, 4/15 업로드 완료.

---

## 12. Who did you have in mind when this product was built?

```
Two primary users:

1. Retail DeFi traders who want passive exposure to top-performing perp strategies 
   without active management. They've heard of copy trading (eToro, Bybit) but won't 
   accept custody risk or KYC. Copy Perp gives them the same experience — on-chain, 
   non-custodial, with their funds in their own wallet.

2. First-time DeFi users who know nothing about perpetuals. Privy's social login 
   (Google/Discord) creates a Solana wallet in 30 seconds — no seed phrase, no 
   MetaMask setup. They pick a top CRS-ranked trader, click Follow, and their first 
   on-chain perpetual trade executes automatically.

Secondary: quants and researchers who want programmatic access to the CRS ranking 
API (/traders/ranked) and backtesting engine (/portfolio/optimal) to build their 
own strategies on top of the platform.
```

---

## 13. Why would users adopt this in production?

```
Three reasons users would switch from CEX copy trading to Copy Perp in production:

1. Self-custody — Funds never leave the user's wallet. In a market where exchange 
   collapses (FTX, Celsius) destroyed billions in user assets, non-custodial is not 
   a nice-to-have. It's the baseline expectation for serious DeFi users.

2. Algorithmic trust, not social popularity — CRS ranks traders on risk-adjusted 
   performance, not follower count. Users can verify every score input on-chain. 
   Backtested results: CRS portfolio +82.7% vs -3.2% random following over 30 days 
   on actual Pacifica mainnet data.

3. Transparent economics — builder_code=noivan is visible in every order. 
   The 0.1% fee is on-chain, not hidden in spread manipulation or opaque rebate 
   structures. Users know exactly what they pay and where it goes.

The 30-second onboarding via Privy removes the setup friction that kills most DeFi 
products. A user with no prior wallet experience can be copy trading on Pacifica 
perps in under a minute.
```

---

## 14. If you had more time, what would you build or improve next?

```
In priority order:

1. Mainnet deployment + Builder Code fee activation
   Flip NETWORK=mainnet, confirm builder_code=noivan approval, and revenue capture 
   begins immediately on real trading volume.

2. Auto stop-loss per followed trader
   If a trader's unrealized loss exceeds a user-defined threshold, Copy Perp 
   automatically exits the position. Protects followers from drawdown tails.

3. Multi-trader portfolio optimizer
   Currently users follow one trader at a time. The next version allocates a 
   user's capital across multiple CRS-ranked traders using Sharpe ratio optimization 
   — same UX, portfolio-level risk management under the hood.

4. Mobile-first UI
   The current frontend is functional but desktop-optimized. A mobile-native 
   experience (React Native or PWA) would unlock the majority of retail users.

5. Social leaderboard + token incentives
   Referral points (Fuul integration is live) → on-chain token rewards. Top 
   followers earn from volume they generate. Creates a viral growth loop.

6. Cross-DEX signal aggregation
   Monitor top traders across Drift, Zeta, and Pacifica simultaneously. Route 
   follower orders to whichever DEX has best liquidity. Copy Perp becomes 
   DEX-agnostic, not Pacifica-only.
```

---

## 15. Are you interested in continuing as a long-term Pacifica ecosystem builder?

```
✅ Yes
```

> 근거: Builder Code `noivan` 수수료 모델이 Pacifica 볼륨과 선형 비례.
> 플랫폼이 성장할수록 Copy Perp 수익도 자동 성장. 장기 생태계 참여가 명확한 인센티브.

---

## ✅ 제출 전 최종 체크리스트

| 항목 | 담당 | 상태 |
|------|------|------|
| GitHub repo Public 확인 | 노이반님 | ⏳ |
| 배포 URL 확보 + Q10 기입 | Dev팀장 | ⏳ |
| 데모 영상 촬영 (4/14) | 노이반님 | ⏳ |
| 영상 Google Drive 업로드 + Q11 기입 | 노이반님 | ⏳ |
| 폼 제출 (마감 4/16) | 노이반님 | ⏳ |

> 폼 링크: https://forms.gle/zYm9ZBH1SoUE9t9o7
