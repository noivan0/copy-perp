# Copy-Perp 트레이더 신뢰성 점수 시스템

> **작성일:** 2026-03-14 | **작성자:** QA팀장
> 리서치 기반: QuantStats metrics, Apesteguia et al. (2020 Mgmt Science), Bybit/Bitget 실무 사례, 메인넷 실데이터 분석

---

## 왜 이 지표가 중요한가? (WHY)

팔로워가 손해를 보는 **핵심 원인 3가지**:
1. **생존자 편향**: 단기 고수익 트레이더는 운이 많고, 지속성이 없다
2. **위험 은폐**: 높은 수익률 뒤에 숨은 극단적 레버리지/드로다운
3. **슬리피지 왜곡**: 대형 트레이더 포지션을 팔로워가 늦게 복사 → 수익 감소

→ 따라서 **단일 지표(수익률만)**로는 절대 신뢰할 수 없음

---

## 핵심 지표 체계: 5차원 평가

### 1️⃣ 수익성 (Profitability) — 30%

| 지표 | 계산법 | 임계값 | 의미 |
|------|--------|--------|------|
| `roi_30d` | (equity_now - equity_30d) / equity_30d | > 5% | 30일 ROI |
| `profit_factor` | Σ수익 / Σ손실 (절대값) | > 1.5 | 수익 지속성 핵심 지표 |
| `avg_win_loss_ratio` | avg_win / avg_loss | > 1.2 | 페이오프 비율 |

**참고:** Apesteguia et al.(2020) - eToro 데이터 분석에서 "과거 수익률만으로 미래 수익 예측은 불가능"
→ 반드시 위험 조정 지표와 함께 사용

---

### 2️⃣ 위험 관리 (Risk Control) — 25%

| 지표 | 계산법 | 임계값 | 의미 |
|------|--------|--------|------|
| `max_drawdown` | (peak - trough) / peak | < 25% | 최대 낙폭 |
| `calmar_ratio` | annualized_return / max_drawdown | > 0.5 | 드로다운 대비 수익 |
| `sharpe_ratio` | (avg_return - rf) / σ(return) | > 1.0 | 변동성 대비 수익 |
| `sortino_ratio` | (avg_return - rf) / σ(neg_return) | > 1.5 | 하락 변동성만 패널티 |

**핵심 인사이트:** Sortino > Sharpe — 상방 변동성은 팔로워에게 좋은 것
Max Drawdown은 **팔로워 심리적 이탈점**과 직결됨 (-30% 이상이면 대부분 이탈)

---

### 3️⃣ 일관성 (Consistency) — 20%

| 지표 | 계산법 | 임계값 | 의미 |
|------|--------|--------|------|
| `win_rate` | 승리 거래수 / 전체 거래수 | > 55% | 기본 승률 |
| `trade_count_30d` | 30일 거래 횟수 | 20~200 | 활동성 (너무 적거나 많으면 의심) |
| `consistency_score` | 월별 플러스 비율 | > 70% | 월별 수익 안정성 |
| `streak_max_loss` | 최대 연속 손실 횟수 | < 5 | 손실 연속성 위험 |

**Liu et al. (2023, SSRN):** 팔로워 이탈율은 트레이더의 연속 손실 기간과 강한 상관관계

---

### 4️⃣ 실행 신뢰성 (Execution Reliability) — 15%

| 지표 | 계산법 | 임계값 | 의미 |
|------|--------|--------|------|
| `avg_position_size_usdc` | 평균 포지션 크기 | 합리적 범위 | 큰 포지션 = 슬리피지 위험 |
| `max_leverage_used` | 최대 레버리지 | < 10x | 극단적 레버리지 위험 |
| `position_hold_time_avg` | 평균 포지션 보유 시간 | > 15분 | 초단타는 복사 불가능 |
| `open_positions_avg` | 평균 동시 오픈 포지션 | 1~10 | 집중도 |

**실무 이슈:** 초단타(< 5분) 트레이더는 복사가 물리적으로 불가능 → **하드 필터링**

---

### 5️⃣ 트랙레코드 (Track Record) — 10%

| 지표 | 계산법 | 임계값 | 의미 |
|------|--------|--------|------|
| `trading_days` | 활동 일수 | > 30일 | 최소 검증 기간 |
| `market_cycle_survived` | 상승장/하락장 모두 플러스? | True/False | 마켓 사이클 검증 |
| `equity_growth_trend` | 자산 성장 추세 (r²) | > 0.6 | 안정적 성장 패턴 |

---

## 종합 신뢰도 점수 (Trust Score) 계산식

```
TrustScore = (
    0.30 * ProfitabilityScore +
    0.25 * RiskScore +
    0.20 * ConsistencyScore +
    0.15 * ExecutionScore +
    0.10 * TrackRecordScore
)

각 점수는 0~100 정규화
```

### 티어 분류

| 티어 | Trust Score | 조건 | 복사 비율 |
|------|------------|------|---------|
| **Tier S** | 80+ | max_dd < 15%, win_rate > 65%, 90일+ 이력 | 최대 10% |
| **Tier A** | 65~79 | max_dd < 25%, win_rate > 55%, 30일+ | 최대 7% |
| **Tier B** | 50~64 | max_dd < 35%, 활성 중 | 최대 5% |
| **Tier C** | < 50 | — | 제외 |

---

## 🚨 즉시 제외 필터 (Hard Filters)

다음 중 하나라도 해당하면 **무조건 제외**:

```python
DISQUALIFY_CONDITIONS = [
    profit_factor < 1.0,          # 총 손실 > 총 수익
    max_drawdown > 50,             # 50% 이상 낙폭
    trade_count_30d < 5,           # 활동 없음
    avg_position_hold_min < 3,     # 초단타 (복사 불가)
    win_rate < 30,                 # 승률 30% 미만
    roi_30d < -20,                 # 30일 -20% 이상 손실
]
```

---

## 메인넷 데이터 적용 결과 (2026-03-14 기준)

| 트레이더 | roi_30d | win_rate | profit_factor | max_dd | Trust Score | Tier |
|---------|---------|----------|---------------|--------|-------------|------|
| HTW-TOP1 | 31.8% | 68% (추정) | 높음 | ~20% | **78** | **A** |
| 5C9-TOP2 | 41.2% | 62% (추정) | 높음 | ~28% | **72** | **A** |
| YjCD-TOP3 | 12.3% | 90% | 1,422,059 (이상치) | 낮음 | **조사필요** | - |
| GTU-BAD1 | -86% | 15% | 0.11 | 높음 | **5** | **제외** |

> ⚠️ `YjCD-TOP3`의 profit_factor=1,422,059는 이상치 — 거래 수 100건 중 loss가 극단적으로 적거나 데이터 오류 가능성. 실제 검증 필요.

---

## 구현 계획

### 단계 1 (MVP): 현재 가능한 지표
- `pnl_30d`, `win_rate`, `trade_count`, `equity` → Pacifica API에서 직접 조회
- 이 4개로 기본 Trust Score 계산 가능

### 단계 2 (v1.1): 추가 지표
- `max_drawdown` 계산 (포지션 히스토리 필요)
- `avg_position_hold_time` (fills 데이터 필요)
- `profit_factor` (개별 trade PnL 필요)

### 단계 3 (v2.0): 고급 지표
- Sortino, Calmar 비율
- 마켓 사이클 검증 (상승장/하락장 구분)
- Monte Carlo 파산 확률 분석

---

## UI/UX 표시 방안

팔로워에게 보여줄 때:
```
[HTW-TOP1]
⭐⭐⭐⭐☆ (Trust: 78/100)

30일 수익: +31.8%    승률: 68%
최대 하락: -20%      안정성: 높음
활동일: 45일          레버리지: 평균 3.2x

📊 수익 안정성 그래프 [▂▄▆█▇▅▆█]
⚠️ 주의: 과거 수익이 미래를 보장하지 않습니다
```
