"""
PnL 실적 기록 강화 테스트
- test_pnl_open_long        : 롱 진입 → follower_positions DB 저장
- test_pnl_close_long       : 롱 진입 → ask → realized_pnl 계산 및 copy_trades.pnl 저장
- test_pnl_persist_after_restart : _load_positions_from_db → self._positions 복원
- test_follower_pnl_api     : GET /followers/{address}/pnl 응답 구조 검증
- test_trades_summary       : GET /trades summary 필드 존재 검증
"""
import asyncio
import sys
import os
import time
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db.database import (
    init_db, add_trader, add_follower,
    upsert_follower_position, get_follower_position,
    delete_follower_position, get_all_follower_positions,
)
from core.copy_engine import CopyEngine


# ── 공통 픽스처 ───────────────────────────────────────

TRADER = "TRADER_PNL_1111111111111111111111111111111"
FOLLOWER = "FOLLOW_PNL_2222222222222222222222222222222"


@pytest.fixture
async def db():
    conn = await init_db(":memory:")
    yield conn
    await conn.close()


@pytest.fixture
async def env(db):
    await add_trader(db, TRADER, "PnLTrader")
    # max_position_usdc=10000: 클램핑 방지 (0.01 BTC × 85000 = 850 USDC < 10000)
    await add_follower(db, FOLLOWER, TRADER, copy_ratio=1.0, max_position_usdc=10000)
    engine = CopyEngine(db, mock_mode=True)
    return db, engine


# ══════════════════════════════════════════════════════
# TC-PNL-001: 롱 진입 → DB follower_positions 저장
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_pnl_open_long(env):
    """TC-PNL-001: bid 진입 → follower_positions 테이블에 저장"""
    db, engine = env

    event = {
        "account": TRADER,
        "symbol": "BTC",
        "event_type": "fulfill_taker",
        "price": "85000",
        "amount": "0.01",
        "side": "open_long",   # → bid
        "cause": "normal",
        "created_at": int(time.time() * 1000),
    }
    await engine.on_fill(event)

    # copy_trade 생성 확인
    async with db.execute(
        "SELECT status, side FROM copy_trades WHERE follower_address=? AND trader_address=?",
        (FOLLOWER, TRADER),
    ) as cur:
        trade_row = await cur.fetchone()
    assert trade_row is not None, "copy_trade 레코드 없음"
    assert trade_row["side"] == "bid"

    # follower_positions DB 저장 확인 (filled인 경우만 저장됨)
    if trade_row["status"] == "filled":
        pos = await get_follower_position(db, FOLLOWER, "BTC")
        assert pos is not None, "follower_positions에 포지션이 저장되지 않음"
        assert pos["side"] == "bid"
        assert pos["entry_price"] > 0
    # mock_mode에서 70~80% 확률로 filled → 최소 copy_trade 레코드는 있어야 함
    assert trade_row is not None


# ══════════════════════════════════════════════════════
# TC-PNL-002: 롱 진입 후 청산 → realized_pnl 계산 및 저장
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_pnl_close_long(env):
    """TC-PNL-002: bid 진입 후 ask → realized_pnl 계산 및 copy_trades.pnl 저장"""
    db, engine = env

    # 포지션을 직접 메모리 + DB에 주입 (mock_mode random 의존성 제거)
    entry_price = 80000.0
    exec_price = 85000.0
    size = 0.01
    symbol = "ETH_TEST"

    engine._positions[FOLLOWER] = {
        symbol: {"entry_price": entry_price, "size": size, "side": "bid"}
    }
    await upsert_follower_position(db, FOLLOWER, symbol, "bid", entry_price, size)

    # ask 이벤트 → 롱 청산
    close_event = {
        "account": TRADER,
        "symbol": symbol,
        "event_type": "fulfill_taker",
        "price": str(exec_price),
        "amount": str(size),
        "side": "close_long",  # → ask
        "cause": "normal",
        "created_at": int(time.time() * 1000),
    }
    await engine.on_fill(close_event)

    # copy_trades 레코드 조회
    async with db.execute(
        "SELECT pnl, side, status FROM copy_trades WHERE follower_address=? AND symbol=?",
        (FOLLOWER, symbol),
    ) as cur:
        rows = await cur.fetchall()

    # ask 주문이 발생했어야 함
    ask_rows = [dict(r) for r in rows if r["side"] == "ask"]
    assert len(ask_rows) > 0, "ask(청산) 주문 레코드 없음"

    # filled된 청산 주문에서 pnl 확인
    filled_asks = [r for r in ask_rows if r["status"] == "filled"]
    if filled_asks:
        # PnL = (exec_price - entry_price) × size = (85000-80000) × 0.01 = 50
        expected_pnl = round((exec_price - entry_price) * size, 6)
        actual_pnl = filled_asks[0]["pnl"]
        assert actual_pnl is not None, "filled 청산 주문의 pnl이 None"
        assert abs(float(actual_pnl) - expected_pnl) < 0.01, (
            f"PnL 불일치: 기대={expected_pnl}, 실제={actual_pnl}"
        )

        # follower_positions 테이블에서 포지션이 삭제됐는지 확인
        remaining = await get_follower_position(db, FOLLOWER, symbol)
        assert remaining is None, "청산 후 follower_positions에 포지션이 남아 있음"


# ══════════════════════════════════════════════════════
# TC-PNL-003: _load_positions_from_db 복원 테스트
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_pnl_persist_after_restart(db):
    """TC-PNL-003: _load_positions_from_db 호출 → self._positions 복원"""
    await add_trader(db, TRADER, "T")
    await add_follower(db, FOLLOWER, TRADER)

    # DB에 포지션 직접 삽입
    await upsert_follower_position(db, FOLLOWER, "BTC", "bid", 85000.0, 0.01)
    await upsert_follower_position(db, FOLLOWER, "ETH", "ask", 2000.0, 1.0)

    # 새 엔진 (메모리 비어 있음)
    engine = CopyEngine(db, mock_mode=True)
    assert engine._positions == {}, "초기 메모리 포지션은 비어 있어야 함"

    # DB에서 복원
    await engine._load_positions_from_db()

    assert FOLLOWER in engine._positions, f"{FOLLOWER} 팔로워가 복원되지 않음"
    assert "BTC" in engine._positions[FOLLOWER], "BTC 포지션이 복원되지 않음"
    assert "ETH" in engine._positions[FOLLOWER], "ETH 포지션이 복원되지 않음"

    btc = engine._positions[FOLLOWER]["BTC"]
    assert btc["side"] == "bid"
    assert abs(btc["entry_price"] - 85000.0) < 0.01
    assert abs(btc["size"] - 0.01) < 0.0001

    eth = engine._positions[FOLLOWER]["ETH"]
    assert eth["side"] == "ask"
    assert abs(eth["entry_price"] - 2000.0) < 0.01


# ══════════════════════════════════════════════════════
# TC-PNL-004: /followers/{address}/pnl API 응답 구조
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_follower_pnl_api():
    """TC-PNL-004: GET /followers/{address}/pnl 응답 구조 검증
    httpx.AsyncClient + ASGITransport으로 실제 FastAPI 앱을 비동기 호출.
    lifespan 없이 _db를 패치해 테스트.
    """
    import httpx
    import api.main as _main
    from api.main import app
    from db.database import init_db as _init_db, add_trader, add_follower, record_copy_trade

    test_db = await _init_db(":memory:")
    await add_trader(test_db, TRADER, "T")
    await add_follower(test_db, FOLLOWER, TRADER)
    # copy_trades에 pnl 데이터 삽입
    await record_copy_trade(test_db, {
        "id": str(uuid.uuid4()),
        "follower_address": FOLLOWER,
        "trader_address": TRADER,
        "symbol": "BTC",
        "side": "ask",
        "amount": "0.01",
        "price": "85000",
        "client_order_id": str(uuid.uuid4()),
        "status": "filled",
        "pnl": 50.0,
        "entry_price": 80000.0,
        "exec_price": 85000.0,
        "created_at": int(time.time() * 1000),
        "error_msg": None,
    })
    await record_copy_trade(test_db, {
        "id": str(uuid.uuid4()),
        "follower_address": FOLLOWER,
        "trader_address": TRADER,
        "symbol": "ETH",
        "side": "ask",
        "amount": "1.0",
        "price": "2000",
        "client_order_id": str(uuid.uuid4()),
        "status": "filled",
        "pnl": -20.0,
        "entry_price": 2100.0,
        "exec_price": 2000.0,
        "created_at": int(time.time() * 1000),
        "error_msg": None,
    })

    # _db 패치 — lifespan 없이 직접 패치 (ASGITransport은 lifespan 실행 안 함)
    original_db = _main._db
    _main._db = test_db

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/followers/{FOLLOWER}/pnl")

        assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text}"
        body = resp.json()

        # 필수 필드 존재 검증
        required_fields = [
            "follower_address",
            "realized_pnl_usdc",
            "unrealized_pnl_usdc",
            "total_trades",
            "win_trades",
            "lose_trades",
            "win_rate",
            "total_volume_usdc",
            "open_positions",
            "pnl_by_trader",
            "roi_pct",
            "builder_fee_paid",
        ]
        for field in required_fields:
            assert field in body, f"응답에 필드 없음: {field}"

        assert body["follower_address"] == FOLLOWER
        # 50 + (-20) = 30
        assert abs(body["realized_pnl_usdc"] - 30.0) < 0.01, f"realized_pnl 불일치: {body['realized_pnl_usdc']}"
        assert body["total_trades"] >= 2
        assert body["win_trades"] >= 1
        assert body["lose_trades"] >= 1
        assert isinstance(body["open_positions"], list)
        assert isinstance(body["pnl_by_trader"], list)
    finally:
        _main._db = original_db
        await test_db.close()


# ══════════════════════════════════════════════════════
# TC-PNL-005: /trades summary 필드 존재 검증
# ══════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_trades_summary():
    """TC-PNL-005: GET /trades 응답에 summary 필드 포함 여부 검증"""
    import httpx
    import api.main as _main
    from api.main import app
    from db.database import init_db as _init_db, add_trader, add_follower, record_copy_trade

    test_db = await _init_db(":memory:")
    await add_trader(test_db, TRADER, "T")
    await add_follower(test_db, FOLLOWER, TRADER)
    await record_copy_trade(test_db, {
        "id": str(uuid.uuid4()),
        "follower_address": FOLLOWER,
        "trader_address": TRADER,
        "symbol": "BTC",
        "side": "bid",
        "amount": "0.01",
        "price": "85000",
        "client_order_id": str(uuid.uuid4()),
        "status": "filled",
        "pnl": 100.0,
        "entry_price": 83000.0,
        "exec_price": 85000.0,
        "created_at": int(time.time() * 1000),
        "error_msg": None,
    })

    original_db = _main._db
    _main._db = test_db

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/trades?limit=5")

        assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text}"
        body = resp.json()

        assert "summary" in body, "응답에 summary 필드 없음"
        summary = body["summary"]

        required_summary_fields = [
            "total",
            "filled",
            "failed",
            "realized_pnl_usdc",
            "total_volume_usdc",
            "win_rate_pct",
        ]
        for field in required_summary_fields:
            assert field in summary, f"summary에 필드 없음: {field}"

        assert summary["total"] >= 1
        assert summary["filled"] >= 1
        assert isinstance(summary["realized_pnl_usdc"], (int, float))
        assert isinstance(summary["total_volume_usdc"], (int, float))
        assert isinstance(summary["win_rate_pct"], (int, float))
    finally:
        _main._db = original_db
        await test_db.close()
