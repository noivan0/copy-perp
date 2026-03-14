# 트레이더 신뢰도 점수 체계 v3 (검증된 지표만)
> 마케팅팀장 | 2026-03-14 | 학술 논문 + 실전 퀀트 + Mainnet 실데이터 기반

---

## 리서치 출처

| 출처 | 내용 |
|------|------|
| Bailey & Lopez de Prado (2012) | Probabilistic Sharpe Ratio (PSR) — 표본 크기/스큐/첨도 보정 Sharpe |
| Jack Schwager | Gain-to-Pain Ratio — 총이익/총손실 (심플하고 강력) |
| quantstats (Anas Roussi) | CPC Index, Common Sense Ratio, Serenity Index, Ulcer Index |
| OKX Copy Trading API | 업계 표준: winRatio, pnlPercentage, profitLossRatio, maxDrawdown, traderPeriod |
| ZuluTrade ZuluRank | 팔로워 실제 수익 반영 + 위험조정 |
| Mainnet 실데이터 검증 | 4명 트레이더 226건 체결 직접 계산 |

---

## 핵심 발견: 단일 지표의 함정

**실제 mainnet 검증 결과:**

| 트레이더 | 리더보드ROI | WR | PF | PSR | Kelly | 판정 |
|---------|-----------|----|----|-----|-------|------|
| YjCD9Gek | 168% | 100% | ∞ | **1.000** | 1.000 | ✅ 신뢰 |
| 4TYEjn9P | 1029% | 80% | 1.09 | **0.545** | 0.065 | ⚠️ 불안정 |
| HtC4WT6J | 109% | 7% | 0.16 | **0.000** | -0.360 | ❌ 차단 |
| E8j5xSbG | 160% | 49% | 0.15 | **0.035** | -2.899 | ❌ 차단 |

→ ROI만 보면 4명 다 좋아 보임. PSR + Kelly가 실제 신뢰도를 드러냄.

---

## 최종 신뢰도 점수 체계 (CARP Score)

**CARP = Consistency · Alpha · Risk · Persistence**

### 축 1: Consistency (일관성) — 30점

```
Probabilistic Sharpe Ratio (PSR)
  - Bailey & Lopez de Prado 방법: 표본 크기 + 스큐 + 첨도 보정
  - PSR < 0.5 → 0점 (통계적 유의미성 없음)
  - PSR 0.5~0.7 → 10점
  - PSR 0.7~0.9 → 20점
  - PSR > 0.9   → 30점

Sortino Ratio (하방 리스크 기준 리스크조정 수익)
  - Sortino < 0   → -5점 (패널티)
  - Sortino 0~0.5 → 5점
  - Sortino > 0.5 → +5점 보너스

Gain-to-Pain Ratio (Jack Schwager)
  - GPR < 1.0 → 0점
  - GPR 1~2   → 5점
  - GPR > 2   → 10점
```

### 축 2: Alpha (수익의 질) — 25점

```
CPC Index = Profit Factor × Win Rate × Risk/Reward
  - 세 가지를 동시에 측정. 하나만 높아서는 안 됨
  - CPC < 0.1  → 0점
  - CPC 0.1~0.5 → 10점
  - CPC 0.5~1.0 → 18점
  - CPC > 1.0   → 25점

Common Sense Ratio = Profit Factor × Tail Ratio
  - 극단 손실 대비 수익성 검증
  - CSR < 1.0 → -5점 패널티
  - CSR > 2.0 → +5점 보너스
```

### 축 3: Risk Control (리스크 관리) — 25점

```
Kelly Criterion
  - Kelly < 0   → 즉시 0점 + TIER D 강제 (기대값 음수)
  - Kelly 0~0.05 → 10점
  - Kelly > 0.05 → 20점

Ulcer Index (DD 깊이 × 지속시간)
  - 단순 MaxDD보다 정확: 자주 드로우다운에 빠지는 트레이더 포착
  - Ulcer 높음 → 패널티 (-5점)
  - Ulcer 낮음 → +5점 보너스

연속 손실 최대값
  - 5건 이상: -5점
  - 10건 이상: -15점
  - 20건 이상: 즉시 TIER D
```

### 축 4: Persistence (지속성 & 표본 신뢰도) — 20점

```
거래 수 × 기간 보정
  - n < 20건  → 0점
  - n 20~50건 → 8점
  - n 50~100건→ 14점
  - n > 100건 → 18점

분석 기간 보정 계수
  - < 7일  → ×0.3 (단기 대박 가능성)
  - 7~30일 → ×0.7
  - 30~90일 → ×0.9
  - > 90일 → ×1.0

Calmar Ratio (CAGR / MaxDD)
  - 장기 복리 vs 드로우다운 균형
  - Calmar < 0 → -5점
  - Calmar > 0.5 → +2점 보너스
```

---

## CARP 종합 판정 기준

```
90~100점 → ✅ TIER A: 추천 (copy_ratio 기본 10~20%)
70~89점  → ⚠️ TIER B: 조건부 (copy_ratio 5% 이하)
50~69점  → 🔶 TIER C: 소액 테스트 ($50 이하)
0~49점   → ❌ TIER D: 팔로우 차단 (리더보드 표시 제한)
```

---

## 노이반님 지표와 CARP 연동

### ADX + Regime 연동
```python
# 체결 품질 가중치
if adx_regime == "trend" and adx > adx_trend_thresh:
    trade_quality_weight = 1.0   # 추세장 + 방향성 확실 → 정상 반영
elif adx_regime == "range":
    trade_quality_weight = 0.5   # 횡보장 진입 → 신뢰도 50% 할인
elif adx_regime == "transition":
    trade_quality_weight = 0.7   # 전환 구간 → 70% 반영

# CARP 계산 시 각 체결에 quality_weight 적용
adjusted_pnl = raw_pnl * trade_quality_weight
```

### Order Flow 연동
```python
# of_dir, of_imb 활용
if of_dir == trade_side:          # OF 방향 = 체결 방향 일치
    signal_quality = "confirmed"  # +10% CARP 보너스
else:
    signal_quality = "against_flow"  # -10% 패널티
    
# score_with_of > score_base → 시장이 확인한 체결
of_bonus_factor = score_with_of / score_base if score_base > 0 else 1.0
```

### Supertrend + BB 연동
```python
# 추세 확인된 체결만 신뢰도 계산에 포함
if supertrend == "UP" and trade_side == "long":
    include_in_reliability = True
elif supertrend == "DOWN" and trade_side == "short":
    include_in_reliability = True
else:
    include_in_reliability = False  # 역추세 체결은 CARP 계산 제외

# BB 과열 구간 진입 체결 → 위험 태그
if bb_pos_ratio > 0.9 or bb_pos_ratio < 0.1:
    risk_tag = "extreme_bb"  # CARP 패널티 적용
```

---

## 팔로워 UX: 신뢰도 표시 방법

```
[4TYEjn9P]  ⚠️ 고위험
CARP Score: 42/100  |  ROI 1029% (리더보드)
실제 신뢰도: ★★☆☆☆
  └ Kelly=0.07 (불안정), PSR=0.55 (경계), MaxDD=121% (위험)
  └ 추천 투자금: $50 이하

[YjCD9Gek]  ✅ 검증됨
CARP Score: 91/100  |  ROI 168% (리더보드)  
실제 신뢰도: ★★★★★
  └ Kelly=1.0 (최상), PSR=1.0 (통계 확신), MaxDD=0% (완벽)
  └ 추천 투자금: 제한 없음
```

---

## 구현 우선순위

**즉시 (Phase 1):**
- Kelly < 0 + PSR < 0.5 → 자동 TIER D 차단
- trades/history에서 실시간 CARP 계산

**1주 내 (Phase 2):**
- ADX/OF/Supertrend 가중치 연동
- CARP 대시보드 (팔로워용)

**데모 전 (Phase 3):**
- Persistence 점수 (30일+ 데이터 누적)
- "이 트레이더의 체결 중 X%가 추세 확인 체결" 표시
- 팔로워 실수익 기반 피드백 루프 (ZuluTrade 방식)
