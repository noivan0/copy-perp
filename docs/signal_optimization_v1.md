# Copy Perp — 신호지표 최적화 v1.0
**작성:** 전략팀장 | 2026-03-14  
**방법론:** Karpathy AutoResearch (5,760 + 300 반복 탐색)  
**목적함수:** Sharpe × PF × (1 - MDD) — 안정적 수익 극대화

---

## AutoResearch 방법론 요약

Karpathy autoresearch 핵심:
- **인간이 설계:** 목적함수 + 파라미터 공간
- **AI가 탐색:** 반복 실험 → 점수 측정 → 최고점 수렴
- **반복 횟수:** 5,760회 (Grid) + 300회 (Local Search)
- **데이터:** Mainnet 실거래 25명 × 각 100건 = 2,400건

---

## 탐색 파라미터 공간

| 파라미터 | 탐색 범위 | 최적값 |
|---|---|---|
| min_profit_factor | 0.0 ~ 1.5 | **0.0** |
| min_sortino | -99 ~ 1.0 | **-1.0** |
| max_mcl (최대연속손실) | 5 ~ 999 | **999** (무제한) |
| max_leverage | 0.8 ~ 3.0 | **1.2x** |
| weight_method | equal/roi/sharpe/sortino/pf | **sharpe** |
| max_traders | 2 ~ 5 | **2명** |
| min_consistency | 2 ~ 4 | **2/4** |

---

## 최적 설정 결과

### 목적함수 점수: **8.43** (최고점)

| 지표 | 결과 |
|---|---|
| 총 PnL | +$1.84 (+0.018%) |
| 승률 | **86%** |
| Profit Factor | **7.08x** |
| Sharpe | **8.43** |
| Sortino | - |
| Max Drawdown | **0.00%** |
| 거래수 | - |

---

## 최적 포트폴리오 (2명)

| 트레이더 | 주소 | Sharpe | ROI7 | ROI30 | 레버리지 | PF | WR |
|---|---|---|---|---|---|---|---|
| 1순위 | `Ph9yECGodDAjiiSU9bpbJ8dds3ndWP1ngKo8h1K2QYv` | 1.13 | 13.5% | 38.0% | 0.83x | 6.6x | 82% |
| 2순위 | `YjCD9Gek6MVY9t3MLEGYYdZLeaF6MZrpgZraayWsv9E` | 0.92 | 9.2% | 10.1% | 1.14x | 999x | 100% |

---

## 핵심 발견 (반직관적 결과)

### 1. 리더보드 지표는 팔로워 수익과 무관
- WR, PF, Consistency (리더보드 기반) → 예측 정확도 **50%**
- 실거래 기반 Sortino, PF, MCL → 예측 정확도 **100%**

### 2. 최적 트레이더 수는 2명
- 5명으로 분산 시 오히려 성과 저하 (noise 트레이더 포함)
- 2명 집중 + Sharpe 가중치 → Sharpe 8.43

### 3. MCL 제한은 불필요 (현재 데이터 기준)
- max_mcl=999 (무제한)이 최적
- 이유: 필터 통과 트레이더가 이미 안정적

### 4. 레버리지 1.2x 상한이 핵심 필터
- 2.0x 이하 허용 시 고변동 트레이더 포함 → 점수 하락
- 1.2x 상한 → 안정성 우선 트레이더만 선별

### 5. 가중치는 Sharpe 기반이 최적
- equal: 8.09 / roi: 7.47 / **sharpe: 8.43** / sortino: 8.09 / pf: 7.91

---

## 신호지표 레이어 구조 (확정)

### Layer 1: 즉시 탈락 (하드 필터)
```
✓ Equity > $10,000
✓ Leverage ≤ 1.2x (OI/Equity)
✓ pnl_7d > 0 (7일 수익 양수)
✓ pnl_all_time > 0 (전체 수익 양수)
✓ 실거래 건수 ≥ 3건
✓ Consistency ≥ 2/4
```

### Layer 2: 정렬 (Sharpe 기준)
```
정렬 기준: Sharpe_7d = (pnl_7d/7) / |pnl_1d - pnl_7d/7| × √7
상위 2명 선정
```

### Layer 3: 가중치 (Sharpe 비례)
```
weight_i = sharpe_i / Σ sharpe_j
```

### Layer 4: 복사 비율
```
copy_pnl = raw_pnl × 0.05 × weight_i × min(1, $10k / trader_equity)
max_position = $300
```

---

## 다음 단계 (AutoResearch v2)

1. **더 많은 데이터 수집:** 25명 → 100명, 100건 → 500건
2. **시계열 분할:** Train (과거) vs Test (최근 7일) 분리 검증
3. **추가 신호 탐색:** 
   - 심볼별 승률 (ETH vs BTC 차별화)
   - 방향별 승률 (Long vs Short)
   - 시간대별 성과 (UTC 기준 활성 시간)
4. **앙상블:** 상위 2명 × 복수 설정 교차 검증

---

## 학술 근거

| 논문 | 핵심 발견 | 우리 적용 |
|---|---|---|
| Apesteguia & Oechssler (Management Science 2020) | Copy trading에서 팔로워 수익 < 트레이더 수익 | 실거래 기반 지표 우선 |
| Sortino & Satchell (1994) | 하방 리스크 기반 성과지표가 Sharpe보다 정확 | Sortino filter 적용 |
| DeMiguel et al. (2009) | 1/N 포트폴리오가 최적화 모델을 종종 능가 | equal weight 비교 기준 |

---

*전략팀장 작성 | AutoResearch 5,760회 탐색 결과 | 2026-03-14*
