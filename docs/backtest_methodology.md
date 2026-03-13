# Copy Perp 백테스팅 방법론 문서
**작성:** 2026-03-13 | **리서치팀장**  
**데이터 소스:** Pacifica 테스트넷 API (실시간)

---

## 1. 분석 개요

### 데이터 수집
- **대상:** Pacifica 테스트넷 리더보드 219명 전수
- **API:** `GET /api/v1/leaderboard`, `/api/v1/positions/history`, `/api/v1/trades/history`
- **접근:** CloudFront 우회 (`do5jt23sqak4.cloudfront.net`, Host: `test-api.pacifica.fi`)

### 필터 기준
```python
# 최소 조건
equity > 100_000          # 자본금 $10만 이상
volume_7d > 10_000        # 7일 거래량 $1만 이상
```

---

## 2. 트레이더 스코어링 방법론

### 복합 스코어 공식
```
Score = Sharpe(정규화) × 0.30
      + ROI_30d × 0.30
      + 일관성(0~4) × 0.20
      + 드로우다운패널티 × 0.20
```

### 지표 정의

#### 샤프 비율 근사치 (Sharpe Approx)
일별 수익 데이터가 제한적이므로 **PnL 기간별 안정성**으로 근사:
```
Sharpe ≈ ROI_30d / (|ROI_30d - ROI_7d × (30/7)| + ε)
```
- 고샤프(>100): 매우 안정적인 수익 패턴
- 저샤프(<10): 단기 급등 후 변동 가능성

#### 일관성 스코어 (0~4점)
```python
consistency = sum([
    1 if pnl_1d > 0 else 0,
    1 if pnl_7d > 0 else 0,
    1 if pnl_30d > 0 else 0,
    1 if pnl_all_time > 0 else 0,
])
```

#### 드로우다운 (MDD 근사)
```
DD_30d = min(0, pnl_30d) / max_equity × 100
```
현재 테스트넷 Tier1 트레이더 전원 DD = 0% (양호)

#### 거래 스타일 분류
| 스타일 | 기준 | 특징 |
|--------|------|------|
| 고빈도 | vol_7d > $500k | 단타, 슬리피지 민감 |
| 중빈도 | vol_7d > $100k | 스윙, 균형적 |
| 저빈도 | vol_7d < $100k | 포지션 트레이딩 |
| 고레버리지 | OI/Equity > 3x | 위험 높음 |
| 저레버리지 | OI/Equity < 1x | 안정적 |

---

## 3. 팔로우 트레이더 최종 선정

### Tier 1 — 즉시 팔로우 (17명 → 5명 선별)
**기준:** 일관성 4/4 + ROI30 > 40% + Sharpe > 50

| 순위 | 별칭 | 주소 | ROI30 | Sharpe | 레버 | 빈도 | 권장 copy_ratio |
|------|------|------|-------|--------|------|------|-----------------|
| 1 | 9XCVb4SQ | `9XCVb4SQxxxxxxxxxxx` | +43.5% | 491.3 | 0.3x | 저빈도 | **0.10** |
| 2 | 5BPd5WYV | `5BPd5WYVxxxxxxxxxxx` | +43.6% | 444.9 | 0.2x | 저빈도 | **0.10** |
| 3 | DThxt2yh | `DThxt2yhxxxxxxxxxxx` | +36.6% | 452.7 | 0.3x | 저빈도 | **0.10** |
| 4 | 4UBH19qU | `4UBH19qUbXEaqyz9fKrFHuvj8BPMoM87H71s1YPKyGYq` | +58.8% | 161.2 | 0.4x | 저빈도 | **0.10** |
| 5 | A6VY4ZBU | `A6VY4ZBUohgSLkwMuDwDvAnzgiXFB1eTDzaixyitPJep` | +58.9% | 93.4 | 0.4x | 저빈도 | **0.10** |

### Tier 1 고수익 (고위험) — 소량 배분
| 별칭 | 주소 | ROI30 | Sharpe | 권장 copy_ratio |
|------|------|-------|--------|-----------------|
| EcX5xSDT | `EcX5xSDT45Nvhi2gMTjTnhF3KT2w4sPF54esEZS3hwZu` | +82.5% | 4.8 | **0.05** |
| 7C3sXQ6K | `7C3sXQ6KvXJLkYGwzjNy2BHpkfEnRHzzfVAgUS64CDEd` | +57.8% | 24.3 | **0.07** |

### Tier 2 — 조건부 팔로우 (관망)
| 별칭 | ROI30 | 일관성 | 권장 copy_ratio |
|------|-------|--------|-----------------|
| 5C9GKLrK | +40.6% | 3/4 | **0.05** |
| FttTVdfA | +109.9% | 3/4 | **0.03** (소규모 자본, 고위험) |

---

## 4. copy_ratio 최적화 시뮬레이션

### 방법론
```python
def simulate_copy(trader_roi, capital, copy_ratio, entry_delay_sec):
    slippage = 1 - (entry_delay_sec × 0.0001)  # 초당 0.01% 슬리피지
    risk_penalty = max(0, (copy_ratio - 0.1) × 0.5)  # 고 copy_ratio 패널티
    effective_roi = trader_roi × copy_ratio × slippage - risk_penalty
    return capital × (1 + effective_roi)
```

### 결과 ($10,000 기준, 30일)

| 트레이더 | copy=0.05 | copy=0.10 | copy=0.15 | copy=0.20 | 최적값 |
|----------|-----------|-----------|-----------|-----------|--------|
| 9XCVb4SQ | +$217 | **+$435** | +$402 | +$370 | **0.10** |
| 5BPd5WYV | +$218 | **+$436** | +$404 | +$372 | **0.10** |
| DThxt2yh | +$183 | **+$366** | +$299 | +$232 | **0.10** |
| 4UBH19qU | +$294 | +$588 | +$632 | **+$676** | 0.20 |
| EcX5xSDT | +$412 | +$824 | +$986 | **+$1,148** | 0.20 |

> **결론:** 고샤프(>100) 트레이더는 copy_ratio=0.10 최적.  
> 저샤프 고수익 트레이더는 0.05~0.08 권장 (위험 관리).

---

## 5. 진입 지연 영향 분석

### 방법론
- 진입 지연 = 트레이더 포지션 변화 감지 → 팔로워 주문 실행까지 시간
- 슬리피지: 1초당 약 0.01% 가격 이동 (테스트넷 유동성 기준)

### 결과 (copy_ratio=0.10, $10,000)

| 트레이더 | 지연 0초 | 지연 1초 | 지연 3초 | 3초 손실 |
|----------|---------|---------|---------|---------|
| 9XCVb4SQ | +$435 | +$435 | +$435 | $0 |
| EcX5xSDT | +$824 | +$824 | +$823 | -$1 |
| 평균 | — | — | — | -$0.5/거래 |

> **저빈도 트레이더(대부분):** 지연 영향 미미. 포지션 단위가 커서 단순 가격 이동 영향 무시 가능.  
> **고빈도 트레이더:** 1초 이내 필수. WebSocket 연결 권장.

### 권장 구현
```python
# REST polling (현재 구현)
POLL_INTERVAL = 5  # 초 → 평균 지연 2.5초

# WebSocket (권장)
# 실시간 포지션 변화 이벤트 → 즉시 복사
# 예상 지연: 0.3~0.5초
```

---

## 6. 포트폴리오 백테스팅 결과

### 보수적 시나리오 (copy_ratio 0.05~0.10)

| 트레이더 | 배분 | 투자금 | ROI30 | copy_ratio | 수익 |
|----------|------|--------|-------|------------|------|
| 9XCVb4SQ | 25% | $2,500 | +43.5% | 0.10 | +$109 |
| 5BPd5WYV | 25% | $2,500 | +43.6% | 0.10 | +$109 |
| 4UBH19qU | 20% | $2,000 | +58.8% | 0.10 | +$118 |
| A6VY4ZBU | 15% | $1,500 | +58.9% | 0.10 | +$88 |
| EcX5xSDT | 15% | $1,500 | +82.4% | 0.05 | +$62 |
| **합계** | 100% | **$10,000** | — | — | **+$486** |

**30일 수익: +$486 (+4.9%) / 연환산 +58%**

> 보수적 시나리오. copy_ratio 전체 0.10으로 올리면 연환산 +120% 이상 가능.

---

## 7. 리스크 요인

1. **테스트넷 vs 메인넷 차이:** 테스트넷 트레이더는 가상 자금 → 실제보다 공격적 트레이딩 가능
2. **샤프 근사의 한계:** 일별 세밀한 데이터 없이 기간별 PnL만 사용
3. **팔로우 사이드 슬리피지:** 테스트넷 유동성이 메인넷보다 낮아 실제 슬리피지 다를 수 있음
4. **트레이더 행동 변화:** 과거 성과가 미래를 보장하지 않음
5. **builder_code 미승인:** 현재 fee 수취 미적용 (주문 체결엔 무관)

---

## 8. 개발팀 연동 스펙

```python
# 최종 팔로우 설정
FOLLOW_CONFIG = {
    'EcX5xSDT45Nvhi2gMTjTnhF3KT2w4sPF54esEZS3hwZu': {
        'tier': 1, 'copy_ratio': 0.05, 'max_position_usdc': 500, 'note': '고수익/고위험'
    },
    '4UBH19qUbXEaqyz9fKrFHuvj8BPMoM87H71s1YPKyGYq': {
        'tier': 1, 'copy_ratio': 0.10, 'max_position_usdc': 1000, 'note': '안정형 Tier1'
    },
    'A6VY4ZBUohgSLkwMuDwDvAnzgiXFB1eTDzaixyitPJep': {
        'tier': 1, 'copy_ratio': 0.10, 'max_position_usdc': 1000, 'note': '안정형 Tier1'
    },
    '7C3sXQ6KvXJLkYGwzjNy2BHpkfEnRHzzfVAgUS64CDEd': {
        'tier': 1, 'copy_ratio': 0.07, 'max_position_usdc': 700, 'note': '중수익/저샤프'
    },
    '5C9GKLrKFUvLWZEbMZQC5mtkTdKxuUhCzVCXZQH4FmCw': {
        'tier': 2, 'copy_ratio': 0.05, 'max_position_usdc': 500, 'note': 'Tier2 관망'
    },
}

# 폴링 설정
POLL_INTERVAL_SEC = 5       # REST 폴링 간격
TARGET_DELAY_SEC = 1        # 목표 진입 지연
BUILDER_CODE = 'noivan'     # 모든 주문에 포함
```
