# Copy Perp 팔로우 트레이더 최종 리스트
**작성:** 2026-03-13 | 리서치팀장  
**기준:** Pacifica 테스트넷 실시간 API 데이터

---

## 🔴 Tier 1 — 즉시 팔로우 (최우선)
**기준:** 일관성 4/4 + ROI30 > 35% + Sharpe > 50

| # | 주소 | ROI30 | ROI7 | Sharpe | 레버 | copy_ratio | Equity |
|---|------|-------|------|--------|------|------------|--------|
| 1 | `9XCVb4SQxxx` | +43.5% | +9.7% | **491.3** | 0.3x | **0.10** | $190,992 |
| 2 | `5BPd5WYVxxx` | +43.6% | +10.5% | **444.9** | 0.2x | **0.10** | $188,687 |
| 3 | `DThxt2yhxxx` | +36.6% | +8.8% | **452.7** | 0.3x | **0.10** | $186,757 |
| 4 | `4UBH19qUbXEaqyz9fKrFHuvj8BPMoM87H71s1YPKyGYq` | +58.8% | +12.4% | 161.2 | 0.4x | **0.10** | $352,783 |
| 5 | `A6VY4ZBUohgSLkwMuDwDvAnzgiXFB1eTDzaixyitPJep` | +58.9% | +11.3% | 93.4 | 0.4x | **0.10** | $276,262 |
| 6 | `E1vabqxixxx` | +47.6% | +12.7% | 104.5 | 0.3x | **0.10** | $191,421 |

### Tier 1 고수익/고위험 (소량 배분)
| # | 주소 | ROI30 | Sharpe | copy_ratio | 비고 |
|---|------|-------|--------|------------|------|
| - | `EcX5xSDT45Nvhi2gMTjTnhF3KT2w4sPF54esEZS3hwZu` | +82.5% | 4.8 | **0.05** | 변동성 높음, 40개 포지션 운용 |
| - | `7C3sXQ6KvXJLkYGwzjNy2BHpkfEnRHzzfVAgUS64CDEd` | +57.8% | 24.3 | **0.07** | Win Rate 19%, 대형 포지션 |

---

## 🟡 Tier 2 — 조건부 팔로우 (관망)
**기준:** 일관성 3/4 + ROI30 > 10%

| 주소 | ROI30 | 일관성 | copy_ratio | 비고 |
|------|-------|--------|------------|------|
| `5C9GKLrKFUvLWZEbMZQC5mtkTdKxuUhCzVCXZQH4FmCw` | +40.6% | 3/4 | 0.05 | Win Rate 13%, 주의 |
| `FttTVdfARe...` | +109.9% | 3/4 | 0.03 | 소자본 $14k, 고위험 |

---

## ⛔ 블랙리스트 (팔로우 제외)
| 주소 | 이유 |
|------|------|
| `ACzEZTgHWB6i9M1eMU5TZiYbGoi2bVrtVywVbH8hS7Cy` | 7일 -$487k, Win Rate 30% |
| 고레버리지(>4x) 트레이더 전체 | 청산 리스크 |

---

## 📊 선정 근거 요약

### 왜 이 5명인가?

**EcX5xSDT** — 가장 높은 ROI(+82.5%), 40개 종목 포트폴리오로 분산 잘 됨.  
단, Sharpe=4.8으로 변동성 있어 copy_ratio 낮게 설정.

**4UBH19qU** — Win Rate 100%(테스트넷), Sharpe 161. 가장 안정적인 고수익 트레이더.  
저빈도+저레버리지 = Copy Perp에 가장 이상적.

**A6VY4ZBU** — Win Rate 92%, 4/4 일관성. 현실적으로 가장 신뢰할 수 있는 트레이더.

**9XCVb4SQ / 5BPd5WYV** — Sharpe 490+. 수익 패턴이 극도로 안정적.  
ROI는 중간이지만 리스크 대비 수익이 가장 우수.

---

## 🔧 개발 연동 코드

```python
# copy_perp/config.py 또는 .env에 적용
FOLLOW_TRADERS = {
    # Tier 1 - 안정형 (Sharpe 기준 상위)
    '9XCVb4SQxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx': {'copy_ratio': 0.10, 'tier': 1},
    '5BPd5WYVxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx': {'copy_ratio': 0.10, 'tier': 1},
    'DThxt2yhxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx': {'copy_ratio': 0.10, 'tier': 1},
    '4UBH19qUbXEaqyz9fKrFHuvj8BPMoM87H71s1YPKyGYq':     {'copy_ratio': 0.10, 'tier': 1},
    'A6VY4ZBUohgSLkwMuDwDvAnzgiXFB1eTDzaixyitPJep':     {'copy_ratio': 0.10, 'tier': 1},
    
    # Tier 1 - 고수익/소량
    'EcX5xSDT45Nvhi2gMTjTnhF3KT2w4sPF54esEZS3hwZu':    {'copy_ratio': 0.05, 'tier': 1},
    '7C3sXQ6KvXJLkYGwzjNy2BHpkfEnRHzzfVAgUS64CDEd':     {'copy_ratio': 0.07, 'tier': 1},
    
    # Tier 2 - 관망
    '5C9GKLrKFUvLWZEbMZQC5mtkTdKxuUhCzVCXZQH4FmCw':    {'copy_ratio': 0.05, 'tier': 2},
}

# 공통 설정
BUILDER_CODE = 'noivan'
MAX_POSITION_USDC = 1000  # 트레이더당 최대 포지션
POLL_INTERVAL = 5  # 초
```

---

*마지막 업데이트: 2026-03-13 | 리서치팀장*
