# 트레이더 성과 신뢰성 평가 설계

> 작성일: 2026-03-14  
> 목적: copy-perp 팔로워 수익 안정성 극대화를 위한 트레이더 선별 방법론

---

## 1. 문제 정의

현재 리더보드는 **pnl_30d (30일 절대 수익)** 만으로 트레이더를 정렬한다.
이 방식의 문제점:

| 함정 | 설명 | 실제 사례 |
|---|---|---|
| **Lucky run** | 단기 대박 후 장기 손실 | pnl30 높지만 pnl7 음수 트레이더 多 |
| **고레버 베팅** | 운 좋으면 순위 상단, 청산 시 팔로워 직격 | 6uC2TdJxxq: lev=11x |
| **절대금액 편향** | 자본이 많으면 절대 수익도 크게 보임 | equity $900k짜리가 top1 차지 |
| **HFT 노이즈** | 거래량 과다 = 슬리피지 전이 불가 | GTU92nBC: turnover=263x |
| **일시적 수익** | 7d 역전 시 팔로워는 하락기 진입 | FN4seJZ9: pnl30+63k, pnl7=-2.6k |

**핵심 인사이트**: pnl_30d 1위(YjCD9Gek, $99k)의 신뢰성 스코어는 0.74로 7위. 
실제 신뢰도 1위는 monogram(0.99) — pnl30 2위지만 모든 기간 일관성 ✅

---

## 2. 신뢰성 스코어 설계 (TraderScore v1)

### 2.1 가용 데이터 (Pacifica Mainnet API)

```
leaderboard: pnl_1d, pnl_7d, pnl_30d, pnl_all_time,
             equity_current, oi_current, volume_30d
positions:   symbol, side, amount, entry_price, funding, liquidation_price
trades:      event_type, price, amount, side, cause, created_at
             (⚠️ symbol 필드 없음)
```

### 2.2 7개 평가 지표

#### (A) 다기간 일관성 (weight: 30%)
```
consistency = count(pnl_1d > 0, pnl_7d > 0, pnl_30d > 0) / 3
```
- 3/3 = 1.0 (모든 기간 수익)  
- 2/3 = 0.67 (단기 조정 허용)
- 1/3 = 0.33 (장기만 수익 — 최근 부진)
- 0/3 = 0.0 (전 기간 손실)

**왜 중요**: 팔로워는 지금 진입한다. 최근 수익이 안 나오면 팔로워도 손실.

#### (B) 최근 모멘텀 (weight: 15%)
```
ratio_7d = pnl_7d / |pnl_30d|
expected = 7/30 ≈ 0.233  # 균등 분포 기준
momentum = clamp(0.5 + (ratio_7d - expected) × 2, 0, 1)
```
- 7일 수익이 30일 대비 가속 중이면 1.0 → 지금이 진입 타이밍
- 감속 중이면 0.0 → 상승세 꺾임

#### (C) ROI 기반 수익률 (weight: 20%)
```
roi_30d = pnl_30d / equity_current
roi_score = clamp(roi_30d / 0.5, 0, 1)  # 50% ROI = 만점
```
절대금액 편향 제거. 자본 $1M에서 $100k 수익(10%)보다  
자본 $100k에서 $80k 수익(80%)이 더 높은 능력.

#### (D) 레버리지 리스크 (weight: 15%)
```
lev = oi_current / equity_current
lev_score = max(0, 1 - lev / 20)  # 20x = 0점
```
- 고레버 트레이더 추종 시 청산 연쇄 위험
- 팔로워는 트레이더보다 늦게 진입 → 레버 리스크 더 큼

#### (E) 자본 충분성 (weight: 10%)
```
cap_score = clamp((equity - 10k) / 90k, 0, 1)
```
- $10k 미만 = 0점 (lucky run 가능성)
- $100k 이상 = 1.0 (검증된 자본 규모)

#### (F) 거래 활성도 (weight: 5%)
```
turnover = volume_30d / equity
# 5x~100x 적정 → 1.0, 이탈 시 패널티
```
- 너무 낮음: 포지션 방치, 복사할 거래가 없음
- 너무 높음(HFT): 슬리피지 전이 불가, 팔로워 복사 지연 문제

#### (G) 올타임 양수 확인 (weight: 5%)
```
alltime_ok = 1.0 if pnl_all_time > 0 else 0.3
```
전체 역사에서 수익: 트레이더 기본 자격 확인

### 2.3 종합 스코어

```python
score = (
    consistency    × 0.30 +
    momentum       × 0.15 +
    roi_score      × 0.20 +
    lev_score      × 0.15 +
    cap_score      × 0.10 +
    activity_score × 0.05 +
    alltime_ok     × 0.05
)
```

---

## 3. 실측 결과 (2026-03-14 기준)

### 현재 pnl_30d 순위 vs 신뢰성 스코어 순위

| 트레이더 | pnl30d 순위 | pnl_30d | 신뢰성 순위 | score | 문제점 |
|---|---|---|---|---|---|
| YjCD9Gek | #1 | $99,718 | #7 | 0.735 | pnl_1d=-$8.3k, 일관성 0.67 |
| monogram | #2 | $73,284 | **#1** | **0.991** | 모든 기간 ✅, ROI 79% |
| Ph9yECGo | #3 | $62,731 | **#2** | **0.900** | 일관성 완벽, 저레버 |
| GTU92nBC | #5 | $54,052 | #16 | 0.694 | turnover=263x (HFT) |
| AEEqcwDj | #6 | $49,218 | #19 | 0.678 | pnl_1d=-$10.6k, lev=5.2x |
| FN4seJZ9 | #3* | $63,870 | 하위 | 낮음 | pnl_7d=-$2.6k ⚠️ |

### 신뢰성 스코어 신규 TOP5 (pnl30d 기준이 아닌 score 기준)

| 순위 | 트레이더 | score | 일관성 | 모멘텀 | ROI% | 레버 |
|---|---|---|---|---|---|---|
| 1 | monogram | 0.991 | 1.00 | 1.00 | 79.4% | 0.0x |
| 2 | Ph9yECGo | 0.900 | 1.00 | 0.72 | 37.1% | 0.9x |
| 3 | 97Nq39YDZq | 0.804 | 1.00 | 0.11 | 44.3% | 1.5x |
| 4 | 5RX2DD425D | 0.764 | 1.00 | 0.44 | 27.5% | 8.2x |
| 5 | YjCD9Gek | 0.735 | 0.67 | 1.00 | 10.7% | 1.0x |

---

## 4. 추가 확보해야 할 데이터 (현재 미제공)

현재 API 제약으로 계산 불가한 지표들 — 중요도 순:

### Priority 1: 승률(Win Rate) & Profit Factor
```
trades API에 symbol 없고 PnL 필드도 없음
→ positions 스냅샷 델타로 근사 계산 필요
  또는 Pacifica에 /stats?account= 엔드포인트 요청
```
**왜 중요**: PF < 1.0 트레이더는 장기 필패. 수익 1회에 손실 여러 번이면 팔로워 불신.

### Priority 2: 최대 드로우다운 (Max Drawdown)
```
일별 equity 스냅샷 누적이 필요
→ 현재 1d/7d/30d PnL만 있음
```
Calmar Ratio = 연환산 수익 / MDD. 높을수록 안정적.

### Priority 3: 포지션 보유 시간 분포
```
positions created_at - updated_at 있음 (활용 가능)
→ 단기 스캘퍼 vs 스윙/포지션 트레이더 구분
```
팔로워 복사 지연(수십 초~수분) 고려 시 스캘퍼 추종은 불리.

### Priority 4: 슬리피지 시뮬레이션
```
트레이더 거래량이 시장 유동성 대비 클수록
팔로워가 같은 가격에 진입 불가
→ 심볼별 order book depth 필요
```

---

## 5. 단계별 구현 로드맵

### Phase 1 (즉시) — 현재 API로 구현 가능
- [x] TraderScore v1 (7개 지표 가중 합산)
- [x] 실시간 트레이더 재선별 (매 폴링마다 리더보드 체크)
- [ ] 포지션 보유시간 필터 (스캘퍼 제외: avg_hold < 10분)

### Phase 2 (단기) — positions 누적으로 추정
- [ ] 승률 추정: positions 스냅샷 비교로 win/loss 감지
- [ ] 7일 equity 시계열 → rolling MDD 계산
- [ ] 심볼별 집중도 리스크 (한 심볼에 몰빵 여부)

### Phase 3 (중기) — 팔로워 체험 시뮬레이션
- [ ] 복사 지연 15초~60초 가정한 슬리피지 모델
- [ ] 팔로워 입장에서의 실제 ROI backtest
- [ ] 트레이더별 "복사가능성 지수" (copyability score)

### Phase 4 (장기) — 온체인 추가 데이터
- [ ] funding rate 부담 (장기 포지션 비용)
- [ ] 청산 이력 (liquidation_price 근접 빈도)
- [ ] 동일 시간대 다른 트레이더와 포지션 상관관계

---

## 6. Papertrading 설계 개선안

### 현재 문제 (v3 기준)
1. trades API에 symbol 없음 → positions 기반만 사용 중
2. 현재 마크가격 없음 → UPnL 실시간 계산 불가
3. positions entry_price = avg price (변동 없음)

### 해결방안
```python
# 마크가격 추정: 이전/현재 폴링 간 entry_price 변화로 추론
# (avg price가 변했다 = 포지션 크기 변화 있음)
# → 새 체결가격 ≈ 새 entry_price로 역산 가능

def estimate_cur_price(old_pos, new_pos):
    old_amt = float(old_pos['amount'])
    new_amt = float(new_pos['amount'])
    old_entry = float(old_pos['entry_price'])
    new_entry = float(new_pos['entry_price'])
    
    if old_amt != new_amt and new_amt > 0:
        # 추가/감소 거래 발생: 새 avg = (old×old_entry + delta×trade_price) / new
        # → trade_price 역산
        delta = new_amt - old_amt
        if delta != 0:
            trade_price = (new_entry * new_amt - old_entry * old_amt) / delta
            return trade_price
    return new_entry
```

---

## 7. 결론 및 권고

**핵심 주장**: `pnl_30d` 순위만으로 트레이더를 선별하면 팔로워는 운좋은 단기 트레이더를 추종하게 된다.

**권고안**:
1. **TraderScore v1** 즉시 적용 — 7개 지표 종합 평가
2. **필터 강화**: score < 0.7 트레이더 제외 (현재 top20 중 절반 탈락)
3. **GTU92nBC 제외**: turnover=263x HFT는 복사 불가
4. **AEEqcwDj 하향 또는 제외**: lev=5.2x + pnl_1d=-$10k 최근 부진
5. **monogram 비중 상향**: 모든 지표 최상위, ROI 79% 실질 수익률 최고
6. **주기적 재선별**: 7일마다 리더보드 재평가, 하락 트레이더 자동 교체

> 이 방식을 적용하면 팔로워가 진입하는 트레이더는  
> "지금도 수익 중이고, 레버리지 낮고, 안정적인 ROI를 내는" 트레이더만 남는다.
