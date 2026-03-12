"""
DB 레이어 자동화 테스트
TC-DB-001 ~ TC-DB-008
"""
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db.database import init_db, add_trader, add_follower, get_followers, get_leaderboard, record_copy_trade
import time, uuid


@pytest.fixture
async def db():
    conn = await init_db(":memory:")
    yield conn
    await conn.close()


# TC-DB-001: 트레이더 등록
@pytest.mark.asyncio
async def test_add_trader(db):
    await add_trader(db, "TRADER001", "Alpha")
    async with db.execute("SELECT * FROM traders WHERE address='TRADER001'") as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["alias"] == "Alpha"


# TC-DB-002: 중복 트레이더 등록 (INSERT OR IGNORE)
@pytest.mark.asyncio
async def test_duplicate_trader_ignored(db):
    await add_trader(db, "TRADER002", "First")
    await add_trader(db, "TRADER002", "Second")  # 중복 — 무시
    async with db.execute("SELECT COUNT(*) as c FROM traders WHERE address='TRADER002'") as cur:
        row = await cur.fetchone()
    assert row["c"] == 1


# TC-DB-003: 팔로워 등록 및 조회
@pytest.mark.asyncio
async def test_add_and_get_follower(db):
    await add_trader(db, "T003")
    await add_follower(db, "F003", "T003", copy_ratio=0.7, max_position_usdc=75)
    rows = await get_followers(db, "T003")
    assert len(rows) == 1
    assert dict(rows[0])["copy_ratio"] == 0.7
    assert dict(rows[0])["max_position_usdc"] == 75


# TC-DB-004: 비활성 팔로워 조회 제외
@pytest.mark.asyncio
async def test_inactive_follower_not_returned(db):
    await add_trader(db, "T004")
    await add_follower(db, "F004A", "T004")
    await add_follower(db, "F004B", "T004")
    await db.execute("UPDATE followers SET active=0 WHERE address='F004A'")
    await db.commit()
    rows = await get_followers(db, "T004")
    addrs = [dict(r)["address"] for r in rows]
    assert "F004A" not in addrs
    assert "F004B" in addrs


# TC-DB-005: copy_trade 기록
@pytest.mark.asyncio
async def test_record_copy_trade(db):
    await add_trader(db, "T005")
    await add_follower(db, "F005", "T005")

    trade = {
        "id": str(uuid.uuid4()),
        "follower_address": "F005",
        "trader_address": "T005",
        "symbol": "BTC",
        "side": "bid",
        "amount": "0.01",
        "price": "85000",
        "client_order_id": str(uuid.uuid4()),
        "status": "filled",
        "created_at": int(time.time() * 1000),
    }
    await record_copy_trade(db, trade)

    async with db.execute("SELECT * FROM copy_trades WHERE trader_address='T005'") as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert dict(rows[0])["symbol"] == "BTC"


# TC-DB-006: 리더보드 정렬 (PnL 내림차순)
@pytest.mark.asyncio
async def test_leaderboard_order(db):
    await add_trader(db, "T006A", "Low")
    await add_trader(db, "T006B", "High")
    await db.execute("UPDATE traders SET total_pnl=100 WHERE address='T006A'")
    await db.execute("UPDATE traders SET total_pnl=500 WHERE address='T006B'")
    await db.commit()

    leaders = await get_leaderboard(db, 10)
    addrs = [dict(r)["address"] for r in leaders]
    assert addrs.index("T006B") < addrs.index("T006A")


# TC-DB-007: 팔로워 없는 트레이더 조회
@pytest.mark.asyncio
async def test_no_followers(db):
    await add_trader(db, "T007_lonely")
    rows = await get_followers(db, "T007_lonely")
    assert rows == []


# TC-DB-008: copy_trade 중복 client_order_id 무시
@pytest.mark.asyncio
async def test_duplicate_order_ignored(db):
    await add_trader(db, "T008")
    await add_follower(db, "F008", "T008")
    coid = str(uuid.uuid4())
    trade = {
        "id": str(uuid.uuid4()),
        "follower_address": "F008",
        "trader_address": "T008",
        "symbol": "SOL",
        "side": "ask",
        "amount": "1.0",
        "price": "87",
        "client_order_id": coid,
        "status": "filled",
        "created_at": int(time.time() * 1000),
    }
    await record_copy_trade(db, trade)
    trade["id"] = str(uuid.uuid4())  # id 달라도
    await record_copy_trade(db, trade)  # client_order_id 동일 → 무시

    async with db.execute("SELECT COUNT(*) as c FROM copy_trades WHERE client_order_id=?", (coid,)) as cur:
        row = await cur.fetchone()
    assert row["c"] == 1, "중복 client_order_id는 1건만 저장되어야 함"
