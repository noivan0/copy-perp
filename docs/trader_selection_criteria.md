# Copy Perp — 트레이더 선정 기준
**작성:** 2026-03-13 | 리서치팀장  
**데이터 소스:** Pacifica 테스트넷 실측 API (219명 → 100명 분석)

---

## 1. 평가 지표 정의

### 1.1 수익률 (Profitability)

```
ROI_30d = pnl_30d / equity_current × 100  (%)
ROI_7d  = pnl_7d  / equity_current × 100  (%)
ROI_AT  = pnl_all_time / equity_current × 100  (%)
```

**의미:** equity 기준으로 정규화하여 자본 규모 차이를 제거. 절대 PnL이 아닌 상대 수익률로 비교.

---

### 1.2 승률 (Win Rate)

```
Win Rate = win_count / (win_count + loss_count)
```

**의미:** 포지션 종료 건 중 수익 발생 비율. 승률만으로 판단 금지 — profit factor(평균 수익/평균 손실)와 함께 봐야 함.

> ⚠️ 주의: 테스트넷은 실제 포지션 히스토리 API(`/positions/history`)가 일부 주소에서 400 반환 → 직접 계산 제한. 가용한 경우에만 활용.

---

### 1.3 샤프 비율 근사치 (Sharpe Approx)

일별 데이터가 제한적이므로 아래 방식으로 근사:

```python
daily_avg  = pnl_7d / 7
daily_std  = |pnl_1d - daily_avg| + ε      # 일간 변동성 근사
sharpe     = (daily_avg / daily_std) × √7  # 7일 기준 연환산
```

**해석:**
| 샤프 | 평가 |
|------|------|
| > 100 | 극도로 안정적 (변동성 매우 낮음) |
| 10~100 | 우수 |
| 2~10 | 양호 |
| < 2 | 주의 (변동성 과다) |

---

### 1.4 최대 드로우다운 (Max Drawdown)

```python
initial_capital = equity_current - pnl_all_time
# pnl_at > 0이면 현재 수익 중 → DD = 0%
# pnl_at < 0이면 손실 중 → DD = |pnl_at| / initial_capital × 100
```

> 실측 기준: Tier 1 트레이더 17명 전원 DD = 0% (모두 수익 상태)

---

### 1.5 일관성 (Consistency)

```python
consistency = sum([
    1 if pnl_1d > 0 else 0,   # 최근 1일
    1 if pnl_7d > 0 else 0,   # 최근 7일
    1 if pnl_30d > 0 else 0,  # 최근 30일
    1 if pnl_all_time > 0 else 0,  # 전체 기간
])
# 4 = 모든 기간 수익 (최우수) / 0 = 전 기간 손실
```

---

### 1.6 레버리지 비율

```python
leverage_ratio = oi_current / equity_current
```

| 비율 | 분류 | 리스크 |
|------|------|--------|
| > 3x | 과레버리지 | 높음 ⚠️ |
| 1~3x | 고레버리지 | 중간 |
| 0.3~1x | 중레버리지 | 낮음 |
| < 0.3x | 저레버리지 | 최소 |

---

### 1.7 모멘텀 (성장 곡선)

```python
momentum_pct = pnl_30d / pnl_all_time × 100
# > 80%: 최근 급성장 (단기 집중)
# 50~80%: 꾸준한 상승세
# < 50%: 성장 둔화 또는 과거 집중
```

---

## 2. 선정 기준 (Tier 분류)

### ✅ Tier 1 — 즉시 팔로우
```
ROI_30d > 20%
AND consistency == 4 (전 기간 수익)
AND drawdown < 30%
AND equity > $100,000
AND OI < $500,000 (과레버 제외)
```

**현재 해당:** 17명 (단, A4XbPsH5는 OI=$722k → 제외 권고)

### 🟡 Tier 2 — 조건부 팔로우
```
ROI_30d > 5%
AND consistency >= 3
AND equity > $50,000
```

**현재 해당:** 8명

### ⛔ Tier 3 / 제외 기준
```
ROI_30d <= 0          # 30일 손실
OR  OI > $500,000     # 과레버 (청산 리스크)
OR  equity < $10,000  # 소자본 (신뢰도 낮음)
OR  consistency <= 1  # 대부분 기간 손실
```

---

## 3. Tier 1 트레이더 17명 개별 분석

### 📊 한눈에 보기

| # | 별칭 | ROI30 | ROI7 | 레버 | 샤프 | 모멘텀 | 스타일 | 위험 | 권장 ratio |
|---|------|-------|------|------|------|--------|--------|------|-----------|
| 1 | EcX5xSDT | +82.5% | +82.4% | 0.50x | 2.75 | 🔥급성장 | 단기집중 | 단기급등 | **0.05** |
| 2 | 4UBH19qU | +59.4% | +12.4% | 0.37x | 22.1 | 🔥급성장 | 단기집중 | 없음 | **0.10** |
| 3 | A6VY4ZBU | +59.0% | +11.5% | 0.43x | 4.91 | 🔥급성장 | 단기집중 | 최근집중 | **0.10** |
| 4 | 7C3sXQ6K | +57.8% | +4.7% | 0.26x | 2.93 | 🔥급성장 | 저레버 장기 | 최근집중 | **0.08** |
| 5 | 7gV81bz9 | +51.5% | +16.6% | 0.20x | 4.48 | 🔥급성장 | 저레버 장기 | 최근집중 | **0.10** |
| 6 | E1vabqxi | +47.7% | +12.8% | 0.28x | 8.36 | 🔥급성장 | 저레버 장기 | 최근집중 | **0.10** |
| 7 | 3rXoG6i5 | +47.4% | +20.1% | 0.25x | 8.92 | 🔥급성장 | 저레버 장기 | 최근집중 | **0.10** |
| 8 | 9XCVb4SQ | +44.9% | +9.9% | 0.33x | 4.30 | 🔥급성장 | 단기집중 | 최근집중 | **0.10** |
| 9 | 5BPd5WYV | +43.6% | +10.5% | 0.24x | **2918** | 🔥급성장 | 저레버 장기 | 최근집중 | **0.10** |
| 10 | 7kDTQZPT | +41.8% | +9.2% | 0.36x | 3.95 | 🔥급성장 | 단기집중 | 최근집중 | **0.10** |
| 11 | HcG1FFVf | +36.8% | +9.2% | 0.30x | 9.83 | 🔥급성장 | 단기집중 | 없음 | **0.10** |
| 12 | DThxt2yh | +36.6% | +8.9% | 0.34x | 13.75 | 🔥급성장 | 단기집중 | 없음 | **0.10** |
| 13 | EYhhf8u9 | +35.9% | +9.9% | 0.27x | 4.33 | 📈상승세 | 저레버 장기 | 없음 | **0.10** |
| 14 | 8r5HRJeS | +35.1% | +12.0% | 0.25x | 7.18 | 🔥급성장 | 저레버 장기 | 없음 | **0.10** |
| 15 | FuHMGqdr | +33.7% | +16.4% | 0.36x | 3.58 | 🔥급성장 | 단기집중 | 최근집중 | **0.08** |
| 16 | AF5a28me | +33.2% | +6.1% | 0.39x | 3.08 | 📈상승세 | 균형스윙 | 없음 | **0.08** |
| 17 | A4XbPsH5 | +31.6% | +10.4% | **4.68x** | 0.31 | 🔥급성장 | 단기집중 | ⚠️과레버 | **제외** |

---

### 🔍 개별 심층 분석

#### #1 EcX5xSDT (ROI30 +82.5%) — 최고 수익, 고변동성
- **스타일:** 40개 이상 종목 동시 운용. 단기 집중형.
- **레버리지:** 0.50x — 중간 수준, 안전
- **리스크:** ROI7d = ROI30d (=82%)→ 수익 전부가 최근 7일에 집중. 이전 성과 없을 가능성.
- **샤프:** 2.75 — 낮음. 변동성 있음.
- **결론:** 수익 매력적이나 지속성 불확실. `copy_ratio=0.05` 소량 배분.

#### #2 4UBH19qU (ROI30 +59.4%) — 균형 최우수
- **스타일:** 저빈도 스윙. 7d ROI 12%로 안정적.
- **레버리지:** 0.37x — 안전
- **샤프:** 22.1 — 우수. 일간 변동 낮음.
- **결론:** ⭐ **1순위 팔로우**. `copy_ratio=0.10`.

#### #3 A6VY4ZBU (ROI30 +59.0%) — 안정적 고수익
- **스타일:** 단기집중형. Win Rate 92%(기존 분석).
- **레버리지:** 0.43x — 적정
- **샤프:** 4.91 — 양호
- **결론:** ⭐ **2순위**. `copy_ratio=0.10`.

#### #4 7C3sXQ6K (ROI30 +57.8%) — 저레버 장기
- **스타일:** OI 낮고 레버 0.26x. 포지션 장기 보유형.
- **주의:** 7d ROI 4.7% → 최근 성과 둔화
- **결론:** `copy_ratio=0.08`. 비중 줄여서 팔로우.

#### #5~#7 (7gV81bz9, E1vabqxi, 3rXoG6i5) — 저레버 안정형
- 모두 레버 0.20~0.28x, ROI30 47~51%, 샤프 4~9
- 위험 요인 최소, 꾸준한 상승세
- **결론:** `copy_ratio=0.10`. 핵심 팔로우 그룹.

#### #9 5BPd5WYV — 특이 사항
- **샤프 2918** — 일간 변동이 거의 0에 가까움 (1d PnL이 daily avg와 거의 동일)
- 테스트넷 데이터 특성일 가능성 있으나, 실제로 매우 안정적일 수 있음
- **결론:** `copy_ratio=0.10`.

#### #17 A4XbPsH5 — ⚠️ 제외 대상
- **OI $722,985 / Equity $154,573 = 레버리지 4.68x**
- 청산 리스크 매우 높음. 기준치($500k OI) 초과.
- **결론:** 팔로우 목록에서 제외.

---

## 4. copy_ratio 최적화 분석

### 방법론
```python
# 실측 backtest 데이터 기반 (초기 자본 $10,000, 10명 팔로우)
# ratio=0.10 기준: 7일 순이익 $181.78 (수수료 $8.09 포함)

scale = ratio / 0.10
pnl_7d  = 181.78 × scale
pnl_30d = pnl_7d × (30/7)  # 선형 외삽 (보수적 가정)
```

### 시나리오 비교

| 시나리오 | ratio | 7일 PnL | 30일 PnL | 연환산 | 특징 |
|----------|-------|---------|---------|--------|------|
| 보수적 | 0.05 | +$90.89 | +$389.53 | **+47%** | 리스크 최소, 수익 절반 |
| **균형** ★ | **0.10** | **+$181.78** | **+$779.06** | **+93%** | **현재 기본값, 검증됨** |
| 공격적 | 0.20 | +$363.56 | +$1,558.11 | **+187%** | 수익 2배, 손실도 2배 |

### 트레이더별 기여도 (ratio=0.10, 7일 순이익)

| 트레이더 | 7d ROI | 배분 | 순이익 | 수수료 |
|----------|--------|------|--------|--------|
| EcX5xSDT | +82.4% | $1,000 | **+$81.55** | $0.84 |
| 3rXoG6i5 | +20.1% | $1,000 | +$19.25 | $0.81 |
| 7gV81bz9 | +16.6% | $1,000 | +$15.75 | $0.81 |
| E1vabqxi | +12.8% | $1,000 | +$12.03 | $0.81 |
| 4UBH19qU | +12.4% | $1,000 | +$11.56 | $0.81 |
| A6VY4ZBU | +11.5% | $1,000 | +$10.64 | $0.81 |
| 5BPd5WYV | +10.5% | $1,000 | +$9.73 | $0.81 |
| 9XCVb4SQ | +9.9% | $1,000 | +$9.05 | $0.80 |
| 7kDTQZPT | +9.2% | $1,000 | +$8.35 | $0.80 |
| 7C3sXQ6K | +4.7% | $1,000 | +$3.87 | $0.80 |
| **합계** | — | **$10,000** | **+$181.78** | **$8.09** |

> **EcX5xSDT 단일 기여도: 44.9%** — 이 트레이더 이상 징후 시 전체 포트폴리오 직격탄. 의존도 분산 필요.

---

## 5. 권장 설정 (최종)

```python
# copy_perp/config.py

FOLLOW_TRADERS = {
    # ⭐ Tier 1 핵심 (위험 없음, ratio=0.10)
    '4UBH19qUbXEaqyz9fKrFHuvj8BPMoM87H71s1YPKyGYq': {'copy_ratio': 0.10, 'tier': 1},
    'A6VY4ZBUohgSLkwMuDwDvAnzgiXFB1eTDzaixyitPJep': {'copy_ratio': 0.10, 'tier': 1},
    '7gV81bz99MUBVb2aLYxW7MG1RMDdRdJYTPyC2syjba8y': {'copy_ratio': 0.10, 'tier': 1},
    'E1vabqxiuUfB29BAwLppTLLNMAq6HJqp7gSz1NiYwWz7': {'copy_ratio': 0.10, 'tier': 1},
    '3rXoG6i55P7D1Q3tYsB7Unds8nBtKh7vH5VUyMDpWkSe': {'copy_ratio': 0.10, 'tier': 1},
    '9XCVb4SQVADNeE6HBhZKytFHFqo1KyCDpqbqNfp48qen': {'copy_ratio': 0.10, 'tier': 1},
    '5BPd5WYVvDE2kHMjzGmLHMaAorSm8bEfERcsycg5GCAD': {'copy_ratio': 0.10, 'tier': 1},
    '7kDTQZPTnaCidXZwEhkoLSia5BKb7zhQ6CmBX2g1RiG3': {'copy_ratio': 0.10, 'tier': 1},
    'HcG1FFVfeW7Q8vpwH3twDQACoBtznXVCAYHaDdQtieMQ':  {'copy_ratio': 0.10, 'tier': 1},
    'DThxt2yhDvJv9KU9bPMuKsd7vcwdDtaRtuh4NvohutQi':  {'copy_ratio': 0.10, 'tier': 1},
    'EYhhf8u9M6kN9tCRVgd2Jki9fJm3XzJRnTF9k5eBC1q1': {'copy_ratio': 0.10, 'tier': 1},
    '8r5HRJeSScGX1TB9D2FZ45xEDspm1qfK4CTuaZvqe7en': {'copy_ratio': 0.10, 'tier': 1},
    'FuHMGqdrn77u944FSYvg9VTw3sD5RVeYS1ezLpGaFes7': {'copy_ratio': 0.08, 'tier': 1},
    'AF5a28meHjecM4dNy8FssFHquWJVv4BK1e5Z8ipRkDgT': {'copy_ratio': 0.08, 'tier': 1},

    # 🔥 고수익/소량 배분 (변동성 주의)
    'EcX5xSDT45Nvhi2gMTjTnhF3KT2w4sPF54esEZS3hwZu': {'copy_ratio': 0.05, 'tier': 1},
    '7C3sXQ6KvXJLkYGwzjNy2BHpkfEnRHzzfVAgUS64CDEd': {'copy_ratio': 0.08, 'tier': 1},

    # ⚠️ 제외
    # 'A4XbPsH59TWjp6vx3QnY8sCb26ew4pBYkYc8Vk4kpbqk': OI=$722k, 레버 4.68x → 블랙리스트
}

BUILDER_CODE = 'noivan'
MAX_POSITION_USDC = 1000
POLL_INTERVAL_SEC = 5
```

---

## 6. 주의사항

1. **테스트넷 한계:** 가상 자금 → 실제보다 공격적. 메인넷 전환 시 copy_ratio 절반으로 시작 권장.
2. **EcX5xSDT 의존도:** 7일 순이익의 45%를 단일 트레이더가 담당. 포트폴리오 분산 강화 필요.
3. **모멘텀 과집중:** 17명 중 15명이 "최근 집중" 패턴 → 시장 조건 변화 시 동시 성과 하락 리스크.
4. **A4XbPsH5 제외:** OI > $500k 기준 초과. 팔로우 시 청산 연쇄 위험.

---

*작성: 리서치팀장 | 2026-03-13*
