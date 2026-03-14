# CopyPerp AutoResearch Program

Karpathy autoresearch 방법론을 copy trading 신호지표 최적화에 적용.

## 목표

**최적의 트레이더 신뢰 지표(CRS)를 자율 실험으로 최적화한다.**

메트릭: `sharpe_approx` (리스크 조정 수익률) — 높을수록 좋음.
보조 메트릭: `roi_pct`, `win_rate`, `profit_factor`, `max_dd_pct`.

## 고정 파일 (수정 금지)

- `papertrading/paper_engine.py` — 시뮬레이션 엔진 (평가 함수)
- `papertrading/run_papertrading.py` — 실행 진입점

## 실험 대상 파일 (수정 가능)

- `core/reliability.py` — **CRS 가중치, 필터 임계값, 지표 공식**
- `core/signal_config.py` — 실험 파라미터 중앙 관리 (새로 생성)

## 실험 루프

1. `signal_config.py`에서 파라미터 1~3개 수정
2. `python3 core/autoresearch/eval.py` 실행 (5분 시뮬레이션)
3. 결과를 `core/autoresearch/results.tsv`에 기록
4. 이전 best보다 sharpe 향상 시 keep, 아니면 revert
5. 반복

## 실험 가능 파라미터

```python
# CRS 가중치 (합 = 1.0)
W_MOMENTUM   = 0.30   # 7d/30d momentum ratio
W_PROFIT     = 0.30   # ROI 기반 수익성
W_RISK       = 0.20   # OI/Equity 리스크
W_CONSISTENCY= 0.20   # 4주 연속 플러스

# 필터 임계값
MIN_MOM_RATIO  = 0.05   # 최소 momentum ratio
MAX_OI_RATIO   = 3.0    # 최대 OI/Equity
MIN_CONSISTENCY= 3       # 최소 consistency score
MIN_ROI_30D    = 0.05   # 최소 30일 ROI (5%)
MAX_ROI_30D    = 1.50   # 최대 30일 ROI (150%, 이상치 제거)

# 복사 비율 설정
TIER_S_COPY = 0.20
TIER_A_COPY = 0.15
TIER_B_COPY = 0.10
```

## 출력 포맷

```
sharpe_approx:  0.8234
roi_pct:        +1.24%
win_rate:       0.612
profit_factor:  2.41x
max_dd_pct:     0.08%
n_traders:      12
```

## 결과 TSV 헤더

```
commit  sharpe  roi_pct  win_rate  profit_factor  max_dd  n_traders  description
```

## 제약 조건

- 실험당 페이퍼트레이딩 5분 (300초) 고정
- API rate limit: 30초 쿨다운 유지
- 트레이더 수 n >= 5 유지 (너무 적으면 샘플 편향)
- max_dd_pct < 2.0% (리스크 한도)

## 논문 기반 가설

1. **Momentum Decay**: 최근 1d 비중을 높이면 추세 추종 개선
   - 근거: Jegadeesh & Titman (1993), crypto momentum 연구
2. **OI/Equity 비선형 패널티**: 2x 이상부터 급격히 패널티
   - 근거: leverage amplification effect
3. **Consistency Bonus**: 4주 연속 양수 트레이더에게 추가 가중치
   - 근거: Apesteguia et al. (2020) — 일관성이 실력의 지표
4. **ROI 구간 최적화**: 극단적 ROI(>100%) 이상치 처리
   - 근거: Survivorship bias in leaderboard data
