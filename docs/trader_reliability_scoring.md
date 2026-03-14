# 트레이더 신뢰성 점수 설계 방법론
## Trader Reliability Scoring for Copy Perp

> 작성일: 2026-03-14  
> 근거: Mainnet 실제 데이터 분석 (상위 트레이더 3명, 각 100건 체결 내역)

---

## 1. 문제 인식: 현재 지표의 함정

### Mainnet 실측 데이터로 확인된 문제점

| 트레이더 | 승률 | PnL All-Time | 실상 |
|---------|------|-------------|------|
| Whale-Alpha | **100%** | $+1.56M | open_long만 100건, 펀딩비 수익자 |
| Multi-Strategy | 52% | $+48K | 16초 간격 고빈도, 수수료 무시 불가 |
| Multi-Pos | 75% | $+85K | 가장 건전한 패턴 |

### 문제 1: 펀딩비 수익자 오탐
- Whale-Alpha: 최근 100건 중 **open_long 58건, close_long 42건** — 숏 없음
- 리더보드 PnL $1.56M의 대부분은 **BTC/ETH 롱 펀딩 수익** (포지션 9개월 보유)
- 팔로워가 복사해도 동일 효과를 내기 어려움 (자본 규모, 펀딩 방향 변화 리스크)

### 문제 2: 리더보드 pnl_all_time의 왜곡
```
리더보드 표시: pnl_all_time = $1,562,589
실제 최근 100건 실현PnL 합계: $1,648 (9시간)
→ 나머지 $1.56M은 미실현 or 펀딩 누적
```

### 문제 3: 고빈도 소액 거래의 착시
- Multi-Strategy: 평균 16초 간격, 수수료 $1.13/100건
- 수수료를 포함한 Expectancy는 긍정적이나 팔로워 슬리피지 추가 시 급감

---

## 2. 신뢰성 지표 체계 (개선안)

### 2.1 기본 5대 지표

#### A. Profit Factor (PF)
```
PF = |총 수익| / |총 손실|
```
- PF ≥ 2.0 : 우량 (팔로워 수수료 감안 후도 흑자 가능)
- PF 1.5~2.0 : 보통
- PF < 1.5 : 위험

**실측:**
- Whale-Alpha: PF = ∞ (손실 없음 → 펀딩비 전략 의심)
- Multi-Strategy: PF = 3.46 ✅
- Multi-Pos: PF = 12.53 ✅✅

#### B. Expectancy Per Trade (EPT)
```
EPT = (승률 × 평균수익) + (패율 × 평균손실)
```
팔로워 관점에서 1거래당 기대수익. **슬리피지 0.05% + 수수료 0.06% = 0.11%** 차감 후 양수여야 의미있음.

```python
ept_net = ept_gross - (avg_position_size * 0.0011)
```

**실측:**
- Whale-Alpha: EPT = $39.25 (단, 펀딩 포함 추정)
- Multi-Strategy: EPT = $0.927 (팔로워 비용 차감 시 마이너스 위험)
- Multi-Pos: EPT = $9.352 ✅

#### C. Trade Sample Size (신뢰구간)
```
최소 표본: 30건 이상
권장: 100건 이상 (신뢰구간 95% 기준)
```
- 30건 미만 → "데이터 부족" 표시, 팔로우 불가
- 표본 수가 많을수록 신뢰도 가중치 증가

#### D. Consistency Score (일관성)
```python
# 월별 / 주별 수익 표준편차
consistency = 1 - (std(monthly_returns) / mean(monthly_returns))
# 0.0~1.0, 높을수록 안정적
```
- 한 달에 $100K 벌고 다음 달에 $-80K 내면 PF는 좋아 보여도 일관성 낮음
- 사용 가능 데이터: `pnl_1d`, `pnl_7d`, `pnl_30d` 3개 시점 비율 분석

```python
def consistency_score(pnl_1d, pnl_7d, pnl_30d):
    # 단기-중기-장기 수익이 동일 방향인지
    if pnl_30d <= 0:
        return 0.0
    short_ratio = pnl_7d / pnl_30d  # 최근 7일이 30일의 몇 %
    score = min(short_ratio, 1.0) if short_ratio > 0 else 0.0
    return score
```

#### E. Strategy Purity (전략 순도)
펀딩비 수익자를 걸러내는 핵심 지표:

```python
def strategy_purity(trades):
    # open_long vs close_long 비율
    opens  = len([t for t in trades if 'open' in t['side']])
    closes = len([t for t in trades if 'close' in t['side']])
    
    # close 비율이 낮으면 포지션 보유 위주 (펀딩비 전략)
    close_ratio = closes / (opens + closes) if (opens + closes) > 0 else 0
    
    # 방향 다양성: 롱/숏 모두 사용하면 +가중치
    longs  = len([t for t in trades if 'long' in t['side']])
    shorts = len([t for t in trades if 'short' in t['side']])
    direction_diversity = min(longs, shorts) / max(longs, shorts) if max(longs, shorts) > 0 else 0
    
    return (close_ratio * 0.6) + (direction_diversity * 0.4)
```

**실측:**
- Whale-Alpha: close_ratio = 42/100 = 0.42, direction = 0/58 = 0 → purity = 0.25 ❌
- Multi-Strategy: close_ratio = 0.58, direction = 39/(39+3) = 0.93 → purity = 0.72 ✅
- Multi-Pos: close_ratio = 100/100 = 1.0, direction = 0 → purity = 0.60 (숏도 하지만 all close) ✅

---

## 3. 복합 신뢰성 점수 (Composite Reliability Score, CRS)

```python
def compute_crs(trader_data: dict) -> float:
    """
    0.0 ~ 100.0 점수
    70+ = 팔로우 추천
    50~70 = 조건부 팔로우
    50 미만 = 팔로우 비추
    """
    # ── 원시 지표 계산 ──
    pf          = min(trader_data['profit_factor'], 20)     # 20 cap
    wr          = trader_data['win_rate'] / 100             # 0~1
    ept         = trader_data['expectancy_per_trade']
    sample_size = trader_data['trade_count']
    purity      = trader_data['strategy_purity']            # 0~1
    consistency = trader_data['consistency_score']          # 0~1
    calmar      = min(trader_data['calmar_ratio'], 50)      # 50 cap
    
    # ── 가중 점수 ──
    score_pf          = (pf / 20) * 25           # 최대 25점
    score_ept         = 15 if ept > 0 else 0     # 양수면 15점
    score_sample      = min(sample_size / 100, 1) * 15  # 100건 = 15점
    score_purity      = purity * 20              # 최대 20점
    score_consistency = consistency * 15         # 최대 15점
    score_calmar      = (calmar / 50) * 10       # 최대 10점
    
    raw = score_pf + score_ept + score_sample + score_purity + score_consistency + score_calmar
    
    # ── 페널티 ──
    # 펀딩비 전략 의심 (purity 낮고 PF 극단적)
    if purity < 0.3 and pf > 15:
        raw *= 0.6
    
    # 데이터 부족 페널티
    if sample_size < 30:
        raw *= 0.5
    
    return min(raw, 100.0)
```

### 실측 적용 결과

| 트레이더 | PF점 | EPT점 | 표본점 | 순도점 | 일관성점 | 칼마점 | **CRS** |
|---------|------|-------|-------|-------|---------|-------|--------|
| Whale-Alpha | 25.0 | 15 | 15 | 5.0 | 10.5 | 10 | **48.5** → 페널티 후 **29.1** |
| Multi-Strategy | 4.3 | 15 | 15 | 14.4 | 7.0 | 0.95 | **56.7** |
| Multi-Pos | 15.7 | 15 | 15 | 12.0 | 12.0 | 10 | **79.7** ✅✅ |

---

## 4. 추가 안전장치 (팔로워 보호)

### 4.1 슬리피지 감응도 분석
```python
def slippage_sensitivity(ept: float, avg_position_usdc: float) -> str:
    """팔로워가 0.1% 슬리피지 추가 시 수익성 유지 여부"""
    slippage_cost = avg_position_usdc * 0.001
    ept_after = ept - slippage_cost
    if ept_after > 0:
        return f"안전 (슬리피지 후 EPT=${ept_after:.2f})"
    else:
        return f"위험 (슬리피지 후 EPT=${ept_after:.2f})"
```

### 4.2 포지션 노출 시간 분석
- 포지션 보유 시간이 너무 길면 → 팔로워가 다른 시점에 복사 시 엔트리 불리
- 최적: 포지션 지속 시간 < 24시간 (단기~중기 트레이더)

### 4.3 최대 낙폭 (MDD) 모니터링
```python
def compute_mdd(cumulative_pnl_series: list) -> float:
    peak = 0
    mdd  = 0
    for pnl in cumulative_pnl_series:
        peak = max(peak, pnl)
        dd   = (peak - pnl) / peak if peak > 0 else 0
        mdd  = max(mdd, dd)
    return mdd * 100  # %
```
- MDD > 30% 트레이더는 자동 경고 플래그

### 4.4 심볼 집중도 (Concentration Risk)
```python
def concentration_risk(trades: list) -> float:
    from collections import Counter
    symbols = Counter(t['symbol'] for t in trades)
    total = sum(symbols.values())
    top1_ratio = symbols.most_common(1)[0][1] / total
    return top1_ratio  # 1.0 = 단일 심볼 집중
```
- 0.8 이상 = 한 심볼에 집중 → 위험 표시

---

## 5. 실시간 적용 계획

### 현재 스코어링 vs 개선안

```python
# 현재 (단순 필터)
score = (pnl7d / equity) * 0.5 + (pnl30d / equity) * 0.5

# 개선안 (CRS 기반)
crs = compute_crs({
    'profit_factor':      compute_profit_factor(trades),
    'expectancy_per_trade': compute_ept(trades),
    'trade_count':        len(trades),
    'strategy_purity':    compute_purity(trades),
    'consistency_score':  compute_consistency(pnl_1d, pnl_7d, pnl_30d),
    'calmar_ratio':       compute_calmar(trades),
    'win_rate':          compute_win_rate(trades),
})
```

### 자동 제외 조건 (Hard Filter)
1. 거래 내역 < 30건 → 자동 제외
2. strategy_purity < 0.2 → 펀딩비 전략, 제외
3. CRS < 50 → 팔로우 불가
4. MDD > 40% → 위험 경고 후 제외
5. 단일 심볼 집중도 > 90% → 다양성 부족 경고

### 등급 체계
| CRS | 등급 | 자동 복사 비율 |
|-----|------|--------------|
| 80+ | S (Elite) | 최대 15% |
| 70~80 | A (Recommended) | 최대 10% |
| 60~70 | B (Standard) | 최대 7% |
| 50~60 | C (Caution) | 최대 5% |
| 50 미만 | D (Excluded) | 팔로우 불가 |

---

## 6. 구현 우선순위

| 우선순위 | 지표 | 난이도 | 효과 |
|---------|------|-------|------|
| 즉시 | Strategy Purity (펀딩비 필터) | 쉬움 | 높음 |
| 즉시 | Expectancy Per Trade | 쉬움 | 높음 |
| 단기 | Trade Sample Size 검증 | 쉬움 | 중간 |
| 단기 | MDD 계산 | 중간 | 높음 |
| 중기 | Consistency Score | 중간 | 중간 |
| 중기 | Calmar Ratio | 중간 | 중간 |
| 장기 | Slippage Sensitivity | 어려움 | 높음 |

---

## 7. 팔로워 수익성 보장 원칙

> "트레이더가 돈을 버는 이유와 팔로워가 돈을 버는 이유는 다를 수 있다"

### 핵심 원칙
1. **펀딩비 수익** → 팔로워에게 그대로 전달 불가능 (자본 규모, 타이밍 차이)
2. **단기 고빈도** → 슬리피지로 팔로워 수익 급감
3. **장기 보유** → 팔로워가 늦게 들어가면 불리한 진입가
4. **이상적 팔로워 적합 트레이더**: 명확한 방향성 트레이딩 + 24시간 이내 청산 + PF > 2.0

### 권장 팔로우 트레이더 프로파일
```
✅ 일일 5~50건 거래
✅ 롱/숏 모두 활용 (방향 불편 없음)
✅ Profit Factor > 2.0
✅ Expectancy > 슬리피지+수수료 비용
✅ MDD < 25%
✅ 100건 이상 거래 내역
❌ open 없이 close만 있는 트레이더 제외
❌ 단일 방향만 거래하는 트레이더 주의
```
