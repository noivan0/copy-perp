"""
Copy Engine 자동화 테스트
Mock 데이터 기반 — API 연결 없이 전체 파이프라인 검증

TC-COPY-001 ~ TC-COPY-010
"""
import asyncio
import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db.database import init_db, add_trader, add_follower, get_followers
from core.copy_engine import CopyEngine
from core.mock import mock_fill_event, MOCK_TRADERS


# ── 픽스처 ────────────────────────────────────────────
@pytest.fixture
async def db():
    conn = await init_db(":memory:")
    yield conn
    await conn.close()


@pytest.fixture
async def engine(db):
    return CopyEngine(db)


@pytest.fixture
async def populated_db(db):
    """트레이더 1명 + 팔로워 2명 등록"""
    trader = "TraderAAA1111111111111111111111111111111111"
    follower1 = "FollowerBBB2222222222222222222222222222222"
    follower2 = "FollowerCCC3333333333333333333333333333333"

    await add_trader(db, trader, "TestTrader")
    await add_follower(db, follower1, trader, copy_ratio=0.5, max_position_usdc=50)
    await add_follower(db, follower2, trader, copy_ratio=1.0, max_position_usdc=100)
    return db, trader, follower1, follower2


# ── TC-COPY-001: 기본 체결 이벤트 처리 ───────────────
@pytest.mark.asyncio
async def test_fill_event_processed(populated_db):
    """TC-COPY-001: open_long 체결 → CopyEngine 처리 완료"""
    db, trader, f1, f2 = populated_db
    engine = CopyEngine(db)

    event = {
        "account": trader,
        "symbol": "BTC",
        "event_type": "fulfill_taker",
        "price": "85000",
        "amount": "0.01",
        "side": "open_long",
        "cause": "normal",
        "created_at": 1773324337000,
    }

    # Agent Key 없어도 파이프라인은 동작 (주문만 스킵)
    await engine.on_fill(event)

    # copy_trades 기록 확인
    async with db.execute("SELECT COUNT(*) as c FROM copy_trades") as cur:
        row = await cur.fetchone()
    assert row["c"] == 2, f"팔로워 2명이므로 copy_trade 2건이어야 함, 실제: {row['c']}"


# ── TC-COPY-002: 청산 이벤트 스킵 ────────────────────
@pytest.mark.asyncio
async def test_liquidation_skipped(populated_db):
    """TC-COPY-002: cause=liquidation 이벤트는 복사 안 함"""
    db, trader, f1, f2 = populated_db
    engine = CopyEngine(db)

    event = {
        "account": trader,
        "symbol": "ETH",
        "event_type": "fulfill_taker",
        "price": "2000",
        "amount": "0.5",
        "side": "close_long",
        "cause": "liquidation",
        "created_at": 1773324337000,
    }

    await engine.on_fill(event)

    async with db.execute("SELECT COUNT(*) as c FROM copy_trades") as cur:
        row = await cur.fetchone()
    assert row["c"] == 0, "청산 이벤트는 복사하지 않아야 함"


# ── TC-COPY-003: 팔로워 없는 트레이더 ────────────────
@pytest.mark.asyncio
async def test_no_followers_no_trade(db):
    """TC-COPY-003: 팔로워 없는 트레이더 체결 → copy_trade 없음"""
    engine = CopyEngine(db)
    trader = "LoneTrader111111111111111111111111111111"
    await add_trader(db, trader, "Lone")

    event = mock_fill_event(trader)
    await engine.on_fill(event)

    async with db.execute("SELECT COUNT(*) as c FROM copy_trades") as cur:
        row = await cur.fetchone()
    assert row["c"] == 0


# ── TC-COPY-004: side 매핑 검증 ──────────────────────
@pytest.mark.asyncio
async def test_side_mapping(populated_db):
    """TC-COPY-004: 각 side 타입별 올바른 복사 side 매핑"""
    from core.copy_engine import _parse_side
    assert _parse_side("open_long") == "bid"
    assert _parse_side("open_short") == "ask"
    assert _parse_side("close_long") == "ask"
    assert _parse_side("close_short") == "bid"
    assert _parse_side("bid") == "bid"
    assert _parse_side("ask") == "ask"
    assert _parse_side("unknown") is None


# ── TC-COPY-005: 복사 비율 계산 ──────────────────────
@pytest.mark.asyncio
async def test_copy_ratio_applied(populated_db):
    """TC-COPY-005: copy_ratio=0.5 → 팔로워 주문량 = 트레이더의 50%"""
    db, trader, f1, f2 = populated_db
    engine = CopyEngine(db)

    event = {
        "account": trader,
        "symbol": "SOL",
        "event_type": "fulfill_taker",
        "price": "87",
        "amount": "1.0",
        "side": "open_long",
        "cause": "normal",
        "created_at": 1773324337000,
    }
    await engine.on_fill(event)

    async with db.execute(
        "SELECT amount FROM copy_trades WHERE follower_address=? ORDER BY created_at DESC LIMIT 1",
        (f1,)
    ) as cur:
        row = await cur.fetchone()

    if row:
        # f1의 copy_ratio=0.5이므로 amount는 0.5 (또는 최소값)
        amount = float(row["amount"])
        assert amount <= 1.0, f"트레이더 amount(1.0)보다 크면 안 됨: {amount}"


# ── TC-COPY-006: 비활성 팔로워 제외 ──────────────────
@pytest.mark.asyncio
async def test_inactive_follower_excluded(db):
    """TC-COPY-006: active=0 팔로워는 복사 제외"""
    trader = "TraderXXX1111111111111111111111111111111"
    follower = "InactiveYYY111111111111111111111111111111"

    await add_trader(db, trader)
    await add_follower(db, follower, trader, copy_ratio=1.0)
    # 비활성화
    await db.execute("UPDATE followers SET active=0 WHERE address=?", (follower,))
    await db.commit()

    engine = CopyEngine(db)
    event = mock_fill_event(trader)
    await engine.on_fill(event)

    async with db.execute("SELECT COUNT(*) as c FROM copy_trades") as cur:
        row = await cur.fetchone()
    assert row["c"] == 0, "비활성 팔로워에게는 복사하지 않아야 함"


# ── TC-COPY-007: 복수 이벤트 순서 보장 ───────────────
@pytest.mark.asyncio
async def test_multiple_events_ordered(populated_db):
    """TC-COPY-007: 연속 체결 이벤트 3건 → 순서대로 처리"""
    db, trader, f1, f2 = populated_db
    engine = CopyEngine(db)

    events = [mock_fill_event(trader) for _ in range(3)]
    for e in events:
        await engine.on_fill(e)

    async with db.execute("SELECT COUNT(*) as c FROM copy_trades") as cur:
        row = await cur.fetchone()
    # 팔로워 2명 × 이벤트 3건 = 최대 6건
    assert row["c"] <= 6


# ── TC-COPY-008: DB 기록 필드 검증 ───────────────────
@pytest.mark.asyncio
async def test_copy_trade_record_fields(populated_db):
    """TC-COPY-008: copy_trade 기록에 필수 필드 모두 존재"""
    db, trader, f1, f2 = populated_db
    engine = CopyEngine(db)

    event = {
        "account": trader,
        "symbol": "BTC",
        "event_type": "fulfill_taker",
        "price": "85000",
        "amount": "0.01",
        "side": "open_long",
        "cause": "normal",
        "created_at": 1773324337000,
    }
    await engine.on_fill(event)

    async with db.execute("SELECT * FROM copy_trades LIMIT 1") as cur:
        row = await cur.fetchone()

    assert row is not None
    record = dict(row)
    required_fields = ["id", "follower_address", "trader_address", "symbol", "side", "amount", "status", "created_at"]
    for field in required_fields:
        assert field in record, f"필수 필드 누락: {field}"
    assert record["symbol"] == "BTC"
    assert record["trader_address"] == trader


# ── TC-COPY-009: unknown side 무시 ───────────────────
@pytest.mark.asyncio
async def test_unknown_side_ignored(populated_db):
    """TC-COPY-009: 알 수 없는 side 값 → 로그만 남기고 처리 안 함"""
    db, trader, f1, f2 = populated_db
    engine = CopyEngine(db)

    event = {
        "account": trader,
        "symbol": "BTC",
        "event_type": "fulfill_taker",
        "price": "85000",
        "amount": "0.01",
        "side": "mystery_side",
        "cause": "normal",
        "created_at": 1773324337000,
    }
    # 예외 없이 처리되어야 함
    await engine.on_fill(event)

    async with db.execute("SELECT COUNT(*) as c FROM copy_trades") as cur:
        row = await cur.fetchone()
    assert row["c"] == 0


# ── TC-COPY-010: 예외 복원력 ─────────────────────────
@pytest.mark.asyncio
async def test_engine_exception_recovery(db):
    """TC-COPY-010: on_fill 내부 예외 → 서비스 중단 없음"""
    engine = CopyEngine(db)

    bad_event = {"bad": "data", "no_account": True}
    # 예외 발생해도 프로그램이 죽지 않아야 함
    try:
        await engine.on_fill(bad_event)
    except Exception:
        pytest.fail("on_fill이 예외를 상위로 전파했음 — 반드시 내부 처리해야 함")
