# Auto Research Loop for Copy Perp Signal

## Karpathy "auto research" 핵심 개념
- 가설 → 실험 → 검증 → 반영의 루프를 자동화
- LLM이 "다음 실험"을 결정, 코드 생성, 실행, 결과 평가

## QuantaAlpha (arxiv 2602.07085) 적용 포인트
1. Trajectory mutation: 지표 조합을 mutation/crossover로 진화
2. Hypothesis-code consistency: 지표 공식 → 코드 → 백테스트 일관성
3. Experience reuse: 좋은 지표 조합 기억, 재활용

## Copy Perp 적용 아키텍처

### 루프 구조
```
[연구 방향 설정] → [가설 생성] → [지표 구현] → [백테스트]
       ↑                                              |
       └──────────── [진화/선택] ←────────────────────┘
```

### 현재 mainnet 데이터로 측정 가능한 지표들
From trades/history: symbol, side, amount, price, entry_price, raw_pnl, created_at

계산 가능:
1. 포지션 크기 (notional)
2. 방향 (long/short)  
3. 보유 시간 (next trade timestamp 기반 추정)
4. 심볼 카테고리 (BTC/ETH 대형 vs 알트 vs meme)
5. 수익/손실 패턴 시퀀스
6. 연속 승/패 패턴
7. 시간대별 성과 (UTC 기준)
8. 포지션 크기 vs 수익률 상관관계

### 자동 진화 목표: CARP Score 가중치 최적화
- W = [w_PF, w_Sharpe, w_Kelly, w_WR, w_MaxDD, w_Recovery]
- 각 가중치 조합 → 트레이더 랭킹 → 해당 랭킹으로 copy → 수익 측정
- 최적 W 탐색

### 검증 기준
- IC (Information Coefficient): 지표값 vs 실제 수익 상관계수
- ICIR: IC / std(IC) — 안정성
- Forward return prediction accuracy
