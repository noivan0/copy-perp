# 트레이더 신뢰성 지표 완전판 (v2)
## Copy Perp — Follower-Centric Reliability Scoring

> 기준: Mainnet 실데이터 + QuantStats / 학술 지표 전수 분석  
> 핵심 원칙: **팔로워가 수익을 내는 지표 ≠ 트레이더가 수익을 내는 지표**

---

## 1. 실데이터로 확인한 진실

| 지표 | Whale-Alpha | Multi-Strategy | Multi-Pos |
|------|------------|---------------|-----------|
| 리더보드 PnL | **$1.56M (1위)** | $48K | $85K |
| 승률 | 100% | 53% | 75% |
| Profit Factor | 999 | 3.61 | **12.53** |
| Sharpe | 2.92 | 0.79 | **7.82** |
| Sortino | 999 | 2.82 | **23.34** |
| Calmar | 999 | 0.009 | 1.05 |
| **Risk of Ruin** | **0%** | **84.6%** | **71.3%** |
| MDD | 0% | **11,274%** | 95% |
| 전략 순도 | 0.23 | 0.62 | **0.73** |
| 결론 | 펀딩비 수익자 | **파산위험 높음** | **팔로우 적합** |

### 핵심 발견: MDD 스케일 문제
- Multi-Strategy의 MDD 11,274% → 초기 자본이 실제로 거의 없는 상태에서 소액 pnl 변동
- PnL 절대값이 아닌 **수익률 기반** MDD 계산이 필수
- Risk of Ruin 71~84%: 두 트레이더 모두 파산 위험 높음 → **포지션 크기 조절이 핵심**

---

## 2. 지표 분류 체계

### Category 1: 수익성 (Profitability) — 돈을 버는가?

#### 1.1 Profit Factor (PF)
```
PF = Σ(수익) / Σ(|손실|)
```
- **기준**: eToro, Bybit, Bitget 모두 사용하는 산업 표준
- **해석**: PF > 2.0이면 손익비 우량. PF = 999는 손실이 없다는 의미 → 의심
- **함정**: 펀딩비 수익자는 PF → ∞ (close 없으므로 손실 기록 없음)

#### 1.2 Expectancy Per Trade (EPT)
```
EPT = (WR × avg_win) + ((1-WR) × avg_loss)
EPT_net = EPT - (avg_pos_size × follower_cost)  # 팔로워 실수익
```
- **핵심**: 팔로워 슬리피지(0.05%) + 수수료(0.06%) = 0.11% 차감 후 **양수 필수**
- 실측: Whale-Alpha $+39.25, Multi-Strategy $+0.98, Multi-Pos $+9.35

#### 1.3 Kelly Criterion (f*)
```
f* = (WR × avg_win - (1-WR) × |avg_loss|) / |avg_loss|
```
- 이론적 최적 베팅 비율 (Ralph Vince, "The Mathematics of Money Management")
- Multi-Pos Kelly = 288% → 이론값 극단, **실용 권장: Kelly × 0.25**
- 실제 copy_ratio = min(kelly × 0.25, 0.15)로 자동 결정

#### 1.4 GHPR (Geometric Holding Period Return)
```
GHPR = [(1+r₁)(1+r₂)...(1+rₙ)]^(1/n) - 1
```
- 복리 효과를 반영한 실질 성장률
- 단순 합산(Expectancy)은 복리 효과를 과대평가함
- Multi-Pos GHPR = 8.75%/trade vs Multi-Strategy 0.66%/trade

---

### Category 2: 위험 조정 수익 (Risk-Adjusted Return) — 리스크 대비 얼마나 버는가?

#### 2.1 Sharpe Ratio
```
Sharpe = (mean_return - Rf) / std_return × √n
```
- 가장 널리 쓰이는 위험조정 지표 (W.F. Sharpe, 1966)
- copy trading 맥락: Rf = 0 (무위험 이자 0 가정)
- **기준**: > 1.0 양호, > 2.0 우량, > 3.0 탁월
- 실측: Multi-Pos = **7.82** (탁월), Multi-Strategy = 0.79 (불량)

#### 2.2 Sortino Ratio
```
Sortino = (mean_return - Rf) / downside_std × √n
```
- **Sharpe 개선판**: 상방 변동성 패널티 없앰 (하방만 페널티)
- 팔로워 관점에서 더 적합 — 수익 변동성은 좋은 것
- 실측: Multi-Pos = **23.34** (극우량)

#### 2.3 Calmar Ratio
```
Calmar = Annualized_Return / MDD
```
- 낙폭 대비 수익률 (Young, 1991)
- **기준**: > 1.0 양호, > 3.0 우량
- 주의: MDD 스케일 문제로 절대값 PnL 기반 Calmar는 왜곡됨

#### 2.4 Ulcer Index (UI)
```
UI = √(mean(drawdown_pct²))
```
- MDD의 지속성과 깊이를 동시에 측정 (Peter Martin, 1987)
- 오래 지속되는 낙폭에 큰 패널티 → 팔로워 심리 안정성 측정

#### 2.5 Ulcer Performance Index (UPI)
```
UPI = mean_return / UI
```
- Sortino의 Ulcer 버전 — 가장 팔로워 친화적 지표

#### 2.6 Common Sense Ratio (CSR)
```
CSR = Profit Factor × Tail Ratio
```
- Tail Ratio = P95 수익 / |P5 손실| — 극단 손실 가능성
- CSR > 1.0 이면 팔로우 적합 (Van Tharp 개념)

---

### Category 3: 위험 (Risk) — 얼마나 위험한가?

#### 3.1 Maximum Drawdown (MDD)
- 가장 직관적인 위험 지표 — **수익률 기반** 계산 필수
- 절대 PnL 기반 MDD는 자본 규모 의존성으로 왜곡

#### 3.2 Recovery Factor (RF)
```
RF = Net Profit / |MDD|
```
- ZuluTrade의 핵심 지표
- RF < 1 : 손실을 회복하지 못함
- RF > 3 : 우량

#### 3.3 Risk of Ruin (RoR)
```
RoR ≈ ((1-WR)/(WR))^(capital/avg_risk)
```
- 파산 확률 (Larry Williams)
- **0%에 가까울수록 좋음**
- RoR > 10% → 팔로우 금지
- 실측: Multi-Strategy 84.6%, Multi-Pos 71.3% → 둘 다 위험!
- 해법: 포지션 크기를 Kelly × 0.25로 제한하면 RoR 급감

#### 3.4 Max Consecutive Losses
- 심리적 저항선 — 팔로워가 포기하기 전까지 버틸 수 있는 연속 손실 횟수
- Multi-Pos: 17연속 손실 → 포지션 크기 작게 유지 필수

---

### Category 4: 전략 순도 (Strategy Purity) — 팔로우 가능한 전략인가?

#### 4.1 Close Ratio (자체 개발)
```
close_ratio = close_trades / total_trades
```
- < 0.4: 펀딩비 전략 의심 (open만 하고 보유)
- 0.4~0.8: 정상 트레이딩
- 1.0: close만 있음 (이전 데이터 없이 청산만 기록됨)

#### 4.2 Direction Diversity
```
direction_div = min(longs, shorts) / max(longs, shorts)
```
- 0.0: 단방향 거래 (롱만 or 숏만)
- 1.0: 완전 균형 (롱=숏)
- 팔로워는 단방향 트레이더보다 양방향 트레이더가 유리

#### 4.3 Position Hold Time
- 평균 보유 시간 > 48h → 팔로워 진입 타이밍 불리
- 이상적: 1~24시간 (팔로워와 가까운 진입가 보장)

---

## 3. 최종 복합 점수 v2 (CRS-v2)

### 가중치 설계 원칙
1. **팔로워 실수익 가능성** > 트레이더 수익
2. **위험 관리** > 수익률
3. **일관성** > 단기 성과
4. **전략 투명성** > 리더보드 순위

```python
def compute_crs_v2(metrics: dict, lb_data: dict) -> dict:
    m = metrics
    
    # ── Layer 1: Hard Filters (실격 조건) ───────────
    flags = []
    
    if m['sample_n'] < 30:
        return {"crs": 0, "grade": "D", "reason": "표본부족"}
    
    if m['strategy_purity'] < 0.25:
        return {"crs": 0, "grade": "D", "reason": "펀딩비전략"}
    
    # EPT 팔로워 비용 차감 후 음수면 팔로우 불가
    follower_cost_usdc = max(abs(m['avg_win']), abs(m['avg_loss'])) * 0.0011
    ept_net = m['expectancy'] - follower_cost_usdc
    if ept_net <= 0:
        flags.append("⚠️ 팔로워 수익 기대값 음수")
    
    # ── Layer 2: 점수 계산 (100점 만점) ─────────────
    
    # A. 수익성 (35점)
    pf_score    = min(m['profit_factor'] / 10, 1.0) * 15   # PF 10 = 만점
    sharpe_score = min(m['sharpe'] / 3, 1.0) * 10          # Sharpe 3 = 만점  
    ept_score   = (15 if ept_net > 0 else 0)               # 팔로워 EPT 양수 여부
    
    # B. 위험조정 (30점)
    sortino_score = min(m['sortino'] / 10, 1.0) * 15       # Sortino 10 = 만점
    csr_score     = min(m['common_sense_ratio'] / 10, 1.0) * 10  # CSR 10 = 만점
    rf_score      = min(m['recovery_factor'] / 5, 1.0) * 5 # RF 5 = 만점
    
    # C. 위험 (20점)
    ror_score  = max(0, 1 - m['risk_of_ruin']/100) * 10   # RoR 0% = 10점
    mdd_score  = max(0, 1 - m['mdd_pct']/100) * 5         # MDD 0% = 5점 (수익률 기반 MDD만)
    consec_score = max(0, 1 - m['max_consec_loss']/20) * 5 # 연속 손실 20 = 0점
    
    # D. 전략 순도 (15점)
    purity_score   = m['strategy_purity'] * 10
    activity_score = min(m['sample_n'] / 100, 1.0) * 5    # 100건 = 5점
    
    raw = (pf_score + sharpe_score + ept_score +
           sortino_score + csr_score + rf_score +
           ror_score + mdd_score + consec_score +
           purity_score + activity_score)
    
    # ── Layer 3: 페널티 ─────────────────────────────
    if m['max_consec_loss'] > 15:
        raw *= 0.85
        flags.append(f"⚠️ 연속손실 {m['max_consec_loss']}회")
    
    if m['risk_of_ruin'] > 20:
        raw *= 0.80
        flags.append(f"⚠️ 파산위험 {m['risk_of_ruin']:.1f}%")
    
    crs = min(max(raw, 0), 100)
    
    # ── 등급 ─────────────────────────────────────────
    if crs >= 80: grade, max_ratio = "S", 0.15
    elif crs >= 70: grade, max_ratio = "A", 0.10
    elif crs >= 60: grade, max_ratio = "B", 0.07
    elif crs >= 50: grade, max_ratio = "C", 0.05
    else: grade, max_ratio = "D", 0.00
    
    # ── 동적 copy_ratio ──────────────────────────────
    # Kelly × 0.25 vs 등급 한도 중 낮은 값
    kelly_safe = m['kelly_pct'] / 100 * 0.25
    recommended_ratio = min(kelly_safe, max_ratio)
    
    return {
        "crs": round(crs, 1),
        "grade": grade,
        "max_copy_ratio": max_ratio,
        "recommended_copy_ratio": round(recommended_ratio, 3),
        "flags": flags,
        "score_breakdown": {
            "profitability": round(pf_score + sharpe_score + ept_score, 2),
            "risk_adjusted": round(sortino_score + csr_score + rf_score, 2),
            "risk":          round(ror_score + mdd_score + consec_score, 2),
            "purity":        round(purity_score + activity_score, 2),
        }
    }
```

---

## 4. CRS-v2 실측 적용 결과

| | Whale-Alpha | Multi-Strategy | Multi-Pos |
|--|------------|---------------|-----------|
| **CRS-v2** | **0 (실격)** | **38.2 (D)** | **62.4 (B)** |
| 실격 사유 | 펀딩비 전략 | RoR 84.6% 페널티 | - |
| 추천 copy_ratio | 0% | 0% | **0.7%** |
| 실수익 EPT | 불가 | -$0.03 (음수) | +$9.24 |

### Multi-Pos가 유일하게 팔로우 가능한 이유
- Sortino 23.34: 수익 방향 변동성은 높지만 손실 변동성 낮음
- CSR 115.3: 극단 수익이 극단 손실보다 9.2배 큼
- EPT $9.35: 팔로워 비용 차감 후도 양수
- 단, RoR 71.3%는 경고 → Kelly × 0.25 = 0.7% 적용

---

## 5. 팔로워 수익성 보장을 위한 추가 레이어

### 5.1 슬리피지 내성 테스트 (Slippage Sensitivity)
```python
def slippage_breakeven(metrics):
    """팔로워가 손익분기점이 되는 최대 슬리피지"""
    ept = metrics['expectancy']
    avg_size = abs(metrics['avg_win'] + metrics['avg_loss']) / 2
    return ept / avg_size  # % 단위
```
- 이 값 이하의 슬리피지면 팔로워 수익 가능

### 5.2 포지션 크기 적응 (Adaptive Sizing)
```python
def adaptive_copy_ratio(crs_result, current_drawdown_pct):
    base = crs_result['recommended_copy_ratio']
    # 낙폭이 커질수록 베팅 축소 (Kelly 원칙)
    if current_drawdown_pct > 20:
        base *= 0.5
    elif current_drawdown_pct > 10:
        base *= 0.75
    return base
```

### 5.3 실시간 성과 감시 (Live Performance Monitor)
- 팔로우 시작 후 10건마다 CRS 재계산
- CRS 10점 이상 하락 → 자동 팔로우 중단
- 연속 손실 5회 → copy_ratio 50% 축소

---

## 6. 업계 비교 (Copy Trading Platform 지표 체계)

| 플랫폼 | 주요 지표 | Copy Perp 차별점 |
|--------|---------|----------------|
| **eToro** | Win Rate, PF, Drawdown, Weeks Trading | + EPT, Sortino, Strategy Purity |
| **Bybit** | ROI, Win Rate, Sharpe, MDD | + Kelly Sizing, RoR |
| **Bitget** | ROI, Win Rate, PF, Followers Profit | + Direction Diversity, Close Ratio |
| **ZuluTrade** | Drawdown, Pips, Recovery Factor | + CSR, Tail Ratio, GHPR |
| **Copy Perp** | **CRS-v2: 모든 지표 통합 + 팔로워 비용 차감** | ✅ 팔로워 수익 직결 |

---

## 7. 구현 우선순위

| 우선순위 | 지표 | 효과 | 난이도 |
|---------|------|------|-------|
| **즉시** | EPT (팔로워 비용 차감) | 팔로워 수익 직결 | 쉬움 |
| **즉시** | Strategy Purity + Close Ratio | 펀딩비 필터 | 쉬움 |
| **즉시** | Risk of Ruin (Hard Filter) | 파산 방지 | 중간 |
| **단기** | Sortino + CSR | 위험조정 정확도 | 중간 |
| **단기** | Kelly-based Adaptive Sizing | 동적 비율 | 중간 |
| **중기** | Slippage Sensitivity | 팔로워 보호 | 어려움 |
| **중기** | Live Performance Monitor | 실시간 감시 | 어려움 |
