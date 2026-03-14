# AutoResearch Program — Copy Perp 신호지표 최적화

## 목표
Pacifica Mainnet 실거래 데이터로 **팔로워 수익률을 최대화**하는
트레이더 신뢰성 지표(CRS) 가중치와 필터 조합을 자율적으로 발견한다.

## 최적화 대상 (에이전트가 수정하는 파일)
- `autoresearch/scorer.py` — CRS 점수 계산 로직 (가중치, 필터 임계값)

## 고정 파일 (수정 금지)
- `autoresearch/evaluate.py` — 평가 루프 (데이터 로드 → 시뮬레이션 → 지표 계산)
- `autoresearch/data/` — 실거래 데이터 (Mainnet 수집)

## 최적화 지표 (낮을수록 좋음)
- **Primary**: `follower_loss` = -EPT_net (팔로워 기대수익 역수)
  - EPT_net = (평균이익 × 승률) - (평균손실 × 패율) - copy_cost(0.11%)
- **Secondary**: `drawdown_penalty` = MDD × 2 (드로우다운 패널티)
- **Score** = follower_loss + drawdown_penalty (낮을수록 좋음)

## 실험 단위
- 각 실험: scorer.py 한 번 수정 → evaluate.py 실행 (5분) → Score 측정
- 개선 시: git commit (keep)
- 악화 시: git revert (discard)

## 에이전트 지침
1. scorer.py의 **가중치(w_*)와 임계값(threshold_*)만 수정**
2. 극단값 금지: 단일 지표 가중치 0.9 초과 불가
3. 매 실험 후 results.jsonl에 기록
4. 3회 연속 개선 없으면 → 새로운 지표 조합 시도
5. 외부 데이터 소스 추가 가능 (논문, arXiv, GitHub에서 수집한 새 지표)

## 현재 베이스라인 (CRS-v2)
```
Profit Factor:   35점 (w=0.35)
Risk-Adjusted:   30점 (w=0.30) — Sortino, CSR, RF
Risk:            20점 (w=0.20) — RoR, MDD, MCL
Purity:          15점 (w=0.15) — 전략순도, 표본수
```

## 우선 탐색 방향
- [ ] 펀딩비 트레이더 자동 탐지 강화 (purity 가중치 상향 검토)
- [ ] 고빈도 트레이더 슬리피지 패널티 추가
- [ ] 팔로워 비용 차감 후 EPT_net > 0 하드 필터
- [ ] 거래 빈도 × 슬리피지 = 실질 복사 가능성 지표

## 리소스 수집 대상 (에이전트가 탐색)
- arXiv: copy trading, alpha decay, trader selection, performance persistence
- GitHub: quantstats, pyfolio, backtrader 지표 구현체
- X/Twitter: @quantopian, @StatArb, @crypto_research 등
