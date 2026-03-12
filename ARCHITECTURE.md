# Copy Perp — Architecture Design
**Track:** 3 (Social & Gamification)  
**컨셉:** Builder Code 기반 탈중앙 카피트레이딩 플랫폼  
**기간:** 3/16 ~ 4/16

---

## 🎯 핵심 플로우

```
트레이더(Leader)                    팔로워(Follower)
     │                                    │
     │  포지션 오픈                         │
     ▼                                    │
Pacifica API ──── WebSocket 이벤트 ────► Copy Engine
                  (fills/positions)           │
                                             │ 비율 계산
                                             │ (팔로워 잔고 × 비율)
                                             ▼
                                    Pacifica API (팔로워 주문)
                                             │
                                             ▼
                                    Builder Code → 수수료 수취
```

---

## 🏗 시스템 아키텍처

```
copy-perp/
├── api/                    # FastAPI 백엔드
│   ├── main.py             # 앱 엔트리포인트
│   ├── routers/
│   │   ├── traders.py      # 트레이더 등록/조회
│   │   ├── followers.py    # 팔로워 등록/설정
│   │   └── stats.py        # 성과 통계
│   └── deps.py             # 의존성
├── core/
│   ├── copy_engine.py      # 핵심 카피 로직
│   ├── position_monitor.py # 트레이더 포지션 감시
│   └── order_executor.py   # 팔로워 주문 실행
├── pacifica/
│   ├── client.py           # REST + WS 클라이언트
│   └── builder_code.py     # Builder Code 수수료 연동
├── db/
│   ├── models.py           # SQLite 스키마
│   └── crud.py             # DB 연산
├── fuul/
│   └── referral.py         # Fuul 레퍼럴 연동
└── frontend/               # 간단한 웹 UI (데모용)
    └── index.html
```

---

## 📐 DB 스키마

```sql
-- 트레이더 (Leader)
CREATE TABLE traders (
    id          TEXT PRIMARY KEY,  -- Solana 주소
    alias       TEXT,              -- 닉네임
    builder_code TEXT,             -- 수수료 수취 코드
    total_pnl   REAL DEFAULT 0,
    win_rate    REAL DEFAULT 0,
    follower_count INT DEFAULT 0,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 팔로워 (Follower)
CREATE TABLE followers (
    id              TEXT PRIMARY KEY,  -- Solana 주소
    trader_id       TEXT REFERENCES traders(id),
    copy_ratio      REAL DEFAULT 1.0,  -- 0.1~1.0 (잔고 비율)
    max_position_usd REAL DEFAULT 100, -- 최대 포지션 크기
    is_active       BOOLEAN DEFAULT true,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 복사된 거래 로그
CREATE TABLE copy_trades (
    id              TEXT PRIMARY KEY,
    trader_id       TEXT,
    follower_id     TEXT,
    original_order_id TEXT,
    copied_order_id   TEXT,
    symbol          TEXT,
    side            TEXT,  -- bid/ask
    trader_amount   REAL,
    follower_amount REAL,
    status          TEXT,  -- pending/filled/failed
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 수수료 수취 기록
CREATE TABLE fee_records (
    id          TEXT PRIMARY KEY,
    trade_id    TEXT REFERENCES copy_trades(id),
    fee_amount  REAL,
    builder_code TEXT,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

---

## ⚙️ Copy Engine 핵심 로직

```python
# core/copy_engine.py 핵심 알고리즘

async def on_trader_fill(event: FillEvent):
    """트레이더 체결 이벤트 수신 시 팔로워 자동 복사"""
    
    trader_id = event.account
    followers = await db.get_active_followers(trader_id)
    
    for follower in followers:
        # 비율 계산
        follower_balance = await pacifica.get_balance(follower.id)
        copy_amount = follower_balance * follower.copy_ratio
        
        # 최대 포지션 제한
        copy_amount = min(copy_amount, follower.max_position_usd)
        
        # 팔로워 주문 실행 (Builder Code 포함)
        order = await pacifica.market_order(
            account=follower.id,
            symbol=event.symbol,
            side=event.side,
            amount=str(copy_amount),
            builder_code=BUILDER_CODE  # 수수료 수취
        )
        
        # 거래 로그 저장
        await db.save_copy_trade(trader_id, follower.id, event, order)
```

---

## 🔌 WebSocket 포지션 감시 전략

SDK에는 `subscribe_prices`만 있지만, 계정 이벤트는 아래 방식으로 구독:

```python
# 방식 1: account_fills 구독 (확인 필요)
ws_message = {
    "method": "subscribe",
    "params": {
        "source": "account_fills",  # 또는 "positions"
        "account": trader_address
    }
}

# 방식 2: REST 폴링 (백업)
# GET /api/v1/positions?account=xxx
# 500ms 간격 폴링으로 변화 감지
```

**W1 Day 1 우선순위:** WS account 이벤트 구독 방식 확인 → 없으면 REST 폴링으로 대체

---

## 🎯 차별화 포인트

1. **Builder Code 수익 구조** — 플랫폼이 카피 거래마다 수수료 수취
2. **Fuul 레퍼럴** — 트레이더가 팔로워 모으면 추가 보상
3. **리더보드** — 트레이더 성과 순위 (Pacifica 리더보드 연동)
4. **Privy 지갑** — 소셜 로그인으로 지갑 없어도 사용 가능 (Best UX Award)
5. **실시간 성과 대시보드** — 팔로워별 수익 현황

---

## 📅 4주 개발 계획

| 주차 | 목표 | 핵심 산출물 |
|------|------|-----------|
| W1 (3/16~22) | 기반 인프라 + WS 연동 | DB 스키마, Copy Engine v1, API 골격 |
| W2 (3/23~29) | 카피 로직 완성 + Builder Code | 자동 카피 동작, 수수료 수취 |
| W3 (3/30~4/5) | 프론트엔드 + Fuul/Privy | 웹 UI, 레퍼럴, 지갑 연동 |
| W4 (4/6~16) | 리더보드 + 안정화 + 제출 | 데모 영상, 문서, 제출 |
