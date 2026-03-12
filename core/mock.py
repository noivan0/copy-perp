"""
Mock 데이터 — API 연결 전 개발/테스트용
실제 API 연결 시 pacifica/client.py로 교체
"""

import random
import time
import uuid

SYMBOLS = ["BTC", "ETH", "SOL", "SUI", "AVAX", "DOGE", "ARB", "OP"]

MOCK_TRADERS = [
    {
        "address": "TraderAAA1111111111111111111111111111111111",
        "alias": "WhaleHunter",
        "win_rate": 0.72,
        "total_pnl": 4820.50,
        "followers": 38,
        "roi_7d": 0.143,
        "max_drawdown": 0.087,
    },
    {
        "address": "TraderBBB2222222222222222222222222222222222",
        "alias": "SolanaKing",
        "win_rate": 0.65,
        "total_pnl": 2310.20,
        "followers": 21,
        "roi_7d": 0.092,
        "max_drawdown": 0.112,
    },
    {
        "address": "TraderCCC3333333333333333333333333333333333",
        "alias": "FundingArb",
        "win_rate": 0.81,
        "total_pnl": 6540.00,
        "followers": 57,
        "roi_7d": 0.218,
        "max_drawdown": 0.043,
    },
    {
        "address": "TraderDDD4444444444444444444444444444444444",
        "alias": "OracleTrader",
        "win_rate": 0.58,
        "total_pnl": 980.75,
        "followers": 12,
        "roi_7d": 0.041,
        "max_drawdown": 0.195,
    },
]

MOCK_MARKET_DATA = {
    sym: {
        "symbol": sym,
        "mark": round(random.uniform(0.5, 100000), 4),
        "oracle": round(random.uniform(0.5, 100000), 4),
        "funding": round(random.uniform(-0.05, 0.05), 6),
        "open_interest": round(random.uniform(1000, 500000), 2),
        "volume_24h": round(random.uniform(10000, 5000000), 2),
        "timestamp": int(time.time() * 1000),
    }
    for sym in SYMBOLS
}

# BTC 고정값 (좀 더 현실적)
MOCK_MARKET_DATA["BTC"].update({
    "mark": 97450.20,
    "oracle": 97380.00,
    "funding": 0.000145,
    "open_interest": 2840000,
})
MOCK_MARKET_DATA["ETH"].update({
    "mark": 2185.40,
    "oracle": 2183.80,
    "funding": -0.000082,
    "open_interest": 890000,
})


def mock_fill_event(trader_address: str) -> dict:
    """무작위 체결 이벤트 생성 (테스트용)"""
    symbol = random.choice(SYMBOLS)
    side = random.choice(["open_long", "open_short", "close_long", "close_short"])
    return {
        "account": trader_address,
        "symbol": symbol,
        "event_type": "fulfill_taker",
        "price": str(round(random.uniform(0.5, 100000), 4)),
        "amount": str(round(random.uniform(0.001, 1.0), 4)),
        "side": side,
        "cause": "normal",
        "created_at": int(time.time() * 1000),
    }


def mock_copy_trade(trader_addr: str, follower_addr: str) -> dict:
    """복사 거래 기록 샘플"""
    symbol = random.choice(SYMBOLS)
    side = random.choice(["bid", "ask"])
    amount = round(random.uniform(0.001, 0.5), 4)
    pnl = round(random.uniform(-50, 150), 2)
    return {
        "id": str(uuid.uuid4()),
        "follower_address": follower_addr,
        "trader_address": trader_addr,
        "symbol": symbol,
        "side": side,
        "amount": str(amount),
        "price": str(round(random.uniform(100, 100000), 2)),
        "client_order_id": str(uuid.uuid4()),
        "status": random.choice(["filled", "filled", "filled", "failed"]),
        "pnl": pnl,
        "created_at": int(time.time() * 1000) - random.randint(0, 86400000),
        "filled_at": int(time.time() * 1000),
    }
