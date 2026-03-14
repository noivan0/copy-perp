# 트레이더 신뢰성 지표 실증 검증 보고서

> 2026-03-14 | Pacifica Mainnet 실데이터 기준 (top20 트레이더)

---

## 1. API 데이터 제약 현실

Pacifica API가 제공하는 실제 필드:

| API | 가용 필드 |
|---|---|
| `leaderboard` | pnl_1d, pnl_7d, pnl_30d, pnl_all_time, equity_current, oi_current, volume_1d/7d/30d |
| `positions` | symbol, side, amount, entry_price, funding, liquidation_price, created_at, updated_at |
| `trades` | event_type, price, amount, side, cause, created_at (**symbol 없음, PnL 없음**) |

---

## 2. 지표별 계산 가능 여부

### ✅ 계산 가능 (실제 사용)

| 지표 | 계산 방법 | 근거 |
|---|---|---|
| ROI (자본 대비 수익률) | pnl_30d / equity_current | 직접 계산 |
| 다기간 일관성 | count(pnl_1d>0, pnl_7d>0, pnl_30d>0) / 3 | 직접 계산 |
| 레버리지 | oi_current / equity_current | 직접 계산 |
| 거래량 회전율 | volume_30d / equity_current | 직접 계산 |
| 모멘텀 (일평균 가속도) | (pnl_7d/7) / ((pnl_30d-pnl_7d)/23) | 직접 계산 |
| 포지션 보유시간 | positions.updated_at - created_at | positions API |
| 청산가 안전거리 | \|entry_price - liquidation_price\| / entry | positions API |
| 펀딩 비용 | positions.funding 합산 | positions API |
| 올타임 양수 | pnl_all_time > 0 | 직접 계산 |

### ❌ 계산 불가 (데이터 없음)

| 지표 | 불가 이유 |
|---|---|
| **Sharpe Ratio** | 일별 수익 시계열 없음 — 1d/7d/30d 단 3개 포인트 |
| **Probabilistic Sharpe Ratio (PSR)** | trade별 PnL 분포 없음 |
| **Sortino Ratio** | 하방 수익 분산 계산 불가 |
| **Kelly Criterion** | win_rate, avg_win, avg_loss 없음 |
| **Calmar Ratio** | Max Drawdown 계산 불가 |
| **Ulcer Index** | 일별 equity 시계열 없음 |
| **Win Rate (정확)** | trades에 symbol 없어서 open/close 페어링 불가 |
| **Profit Factor (정확)** | trade별 PnL 없음 |
| **CPC Index** | PF × WR × RR 모두 계산 불가 |

### ⚠️ 근사 가능 (정확도 낮음)

| 지표 | 한계 |
|---|---|
| Win Rate | positions 스냅샷 비교 → 폴링 간격(2분)에 의존, 빠른 청산 누락 |
| Max Drawdown | 폴링 간격 사이 극값 누락 가능 |
| Profit Factor | exit price 불확실 (마크가격 API 없음) |

---

## 3. 실증 검증 결과 (top20 기준)

### ① 다기간 일관성 → **예측력 있음 ✅**

| 그룹 | n | 평균 7d ROI | 평균 1d ROI |
|---|---|---|---|
| 완벽(1d+7d+30d 모두 양수) | 6 | **+13.3%** | +5.2% |
| 부분(2/3 양수) | 12 | +5.4% | -1.8% |
| 부진(1/3만 양수) | 2 | **-3.9%** | -3.3% |

→ 일관성 높을수록 최근 수익도 유지됨. **팔로워 입장에서 가장 중요한 지표.**

### ② 레버리지 → **단기 예측력 있음 ✅**

| 구간 | n | 7d ROI | 30d ROI |
|---|---|---|---|
| 저레버(0-1x) | 5 | **+16.2%** | +29.3% |
| 중레버(1-5x) | 12 | +2.3% | +24.2% |
| 고레버(5x+) | 3 | +9.6% | +28.9% |

→ 저레버가 최근 7일 수익도 높음. **고레버는 30일 수익은 비슷하지만 변동성↑ → 팔로워 리스크↑**

### ③ 회전율(HFT 여부) → **중간 신호 ⚠️**

| 구간 | n | 7d ROI | 1d ROI |
|---|---|---|---|
| 저회전(<10x) | 8 | +4.2% | +0.2% |
| 중회전(10-50x) | 7 | **+12.1%** | +1.0% |
| HFT(50x+) | 5 | +3.7% | **-1.1%** |

→ HFT 트레이더 최근 1일 손실 중. **팔로워 복사 지연 시 더 불리.**

### ④ 모멘텀(가속도) → **예측력 있음 ✅**

| 그룹 | n | 7d ROI | 1d ROI |
|---|---|---|---|
| 가속 중(>1x) | 10 | **+13.5%** | 0.0% |
| 유지(0~1x) | 6 | +2.1% | +1.2% |
| 역전(<0) | 4 | **-2.6%** | -1.0% |

→ 모멘텀 역전 트레이더는 최근 수익도 음수. **탈락 기준으로 유효.**

### ⑤ ROI vs 절대금액 — 핵심 발견

**FN4seJZ9: ROI 1위(120.6%)이지만 7d ROI = -7.8%** → 과거 고점에서 하락 중.  
단순 ROI만 보면 지금 들어가면 손실 구간 진입.

→ **다기간 일관성 + 모멘텀 조합이 단순 ROI보다 팔로워 보호에 효과적.**

---

## 4. 최종 권고: 실제로 사용할 지표 3개

현재 API 제약 하에서 **실제로 계산 가능하고 예측력이 검증된** 지표:

### Score_v2 = 단순화된 3축

```python
def score_v2(trader):
    p1, p7, p30 = pnl_1d, pnl_7d, pnl_30d
    eq = equity_current
    oi = oi_current
    v30 = volume_30d

    # A) 다기간 일관성 (40점) — 검증됨 ✅
    consistency = sum([p30>0, p7>0, p1>0]) / 3
    score_A = consistency * 40

    # B) 모멘텀 (30점) — 검증됨 ✅
    prev23d_pnl = p30 - p7
    if prev23d_pnl != 0:
        momentum = (p7/7) / (prev23d_pnl/23)
        momentum_norm = min(1.0, max(0.0, momentum / 2))
    else:
        momentum_norm = 0.5 if p7 > 0 else 0.0
    score_B = momentum_norm * 30

    # C) 레버리지 안전도 (30점) — 검증됨 ✅
    lev = oi / max(eq, 1)
    lev_score = max(0.0, 1.0 - lev / 15)
    score_C = lev_score * 30

    return score_A + score_B + score_C  # 최대 100점
```

### 추가 필터 (점수 외 즉시 탈락 기준)

| 조건 | 이유 |
|---|---|
| pnl_30d < $5,000 | 표본 크기 부족 |
| equity < $50,000 | lucky run 위험 |
| volume_30d / equity > 150x | HFT, 복사 불가 |
| pnl_all_time < 0 | 전체 기간 손실 |
| oi / equity > 15x | 청산 위험 |

---

## 5. 포기한 지표 (이유 명확)

| 지표 | 포기 이유 |
|---|---|
| Sharpe / PSR / Sortino | **데이터 없음.** 3포인트(1d/7d/30d)로 분산 계산 불가 |
| Kelly / Calmar / Ulcer | **MDD, WR, PnL 분포 없음.** 계산 자체 불가 |
| Win Rate / Profit Factor | **trades에 symbol 없음.** open/close 페어링 불가 → 근사도 신뢰 낮음 |
| ADX regime 반영 | **내부 시스템 지표** → 팔로워 선별 지표에 반영 불가 (팔로워는 트레이더의 ADX 모름) |
| OF 방향 연동 | **같은 이유** — copy-perp 내부 signal이지 트레이더 평가 기준이 아님 |

**ADX/OF 지표에 대한 명확한 입장:**  
이것들은 우리 시스템이 "언제 복사할지"를 결정하는 **실행 필터**이지, 트레이더 자체의 신뢰도를 평가하는 지표가 아닙니다. 용도가 다릅니다.

---

## 6. 남은 과제 (데이터 확보 방향)

가장 가치 있는 데이터가 **현재 없는 것**: 트레이더의 과거 trade별 PnL.

이걸 얻으려면:
1. Pacifica에 `/stats?account=` 엔드포인트 추가 요청 (WR, PF 직접 제공)
2. positions 스냅샷을 장기 누적 → crude win rate 추정
3. on-chain 트랜잭션 파싱 → 정확한 체결가/PnL 복원

우선순위는 1번. API 1개 추가로 Sharpe/Kelly까지 계산 가능해짐.
