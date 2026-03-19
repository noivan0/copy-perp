"""
팔로워 PnL 실적 기록/조회 테스트
test_pnl_summary_empty, test_pnl_summary_with_trades, test_pnl_by_trader,
test_pnl_history, test_snapshot_follower_pnl, test_pnl_trades_pagination
"""
import pytest
import sys
import os
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db.database import init_db, add_trader, add_follower, record_copy_trade
from db.models import DB


# ── 헬퍼 ──────────────────────────────────────────────────────────────────────

def _make_trade(
    follower: str,
    trader: str,
    pnl: float = None,
    symbol: str = "BTC",
    amount: str = "0.01",
    price: str = "80000",
    status: str = "filled",
    ts: int = None,
) -> dict:
    if ts is None:
        ts = int(time.time() * 1000)
    return {
        "id": str(uuid.uuid4()),
        "follower_address": follower,
        "trader_address": trader,
        "symbol": symbol,
        "side": "bid",
        "amount": amount,
        "price": price,
        "client_order_id": str(uuid.uuid4()),
        "status": status,
        "pnl": pnl,
        "entry_price": float(price),
        "exec_price": float(price),
        "error_msg": None,
        "created_at": ts,
    }


# ── fixture ──────────────────────────────────────────────────────────────────

@pytest.fixture
async def db():
    """in-memory aiosqlite 연결 (database.py 기반)"""
    conn = await init_db(":memory:")
    yield conn
    await conn.close()


@pytest.fixture
async def model_db():
    """in-memory DB 를 DB 모델 클래스로 감싸기"""
    m = DB(":memory:")
    await m.init()
    return m


# ── TC-PNL-001: 거래 없는 팔로워 → 0 반환 ────────────────────────────────────

@pytest.mark.asyncio
async def test_pnl_summary_empty(db):
    await add_trader(db, "T_EMPTY")
    await add_follower(db, "F_EMPTY", "T_EMPTY")

    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")

    async with db.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN pnl IS NOT NULL THEN pnl ELSE 0 END), 0) as total_pnl,
            COUNT(*) as total_trades,
            SUM(CASE WHEN pnl IS NOT NULL AND pnl > 0 THEN 1 ELSE 0 END) as win_count
        FROM copy_trades
        WHERE follower_address = ?
          AND status = 'filled'
          AND date(created_at / 1000, 'unixepoch') >= ?
    """, ("F_EMPTY", cutoff)) as cur:
        row = dict(await cur.fetchone())

    assert float(row["total_pnl"]) == 0.0
    assert int(row["total_trades"]) == 0
    assert (row["win_count"] or 0) == 0


# ── TC-PNL-002: 수익/손실 거래 혼합 → 정확한 집계 ───────────────────────────

@pytest.mark.asyncio
async def test_pnl_summary_with_trades(db):
    await add_trader(db, "T_MIXED")
    await add_follower(db, "F_MIXED", "T_MIXED")

    trades = [
        _make_trade("F_MIXED", "T_MIXED", pnl=100.0),   # 수익
        _make_trade("F_MIXED", "T_MIXED", pnl=-30.0),   # 손실
        _make_trade("F_MIXED", "T_MIXED", pnl=50.0),    # 수익
        _make_trade("F_MIXED", "T_MIXED", pnl=-10.0),   # 손실
    ]
    for t in trades:
        await record_copy_trade(db, t)

    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")

    async with db.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN pnl IS NOT NULL THEN pnl ELSE 0 END), 0) as total_pnl,
            COUNT(*) as total_trades,
            SUM(CASE WHEN pnl IS NOT NULL AND pnl > 0 THEN 1 ELSE 0 END) as win_count,
            SUM(CASE WHEN pnl IS NOT NULL AND pnl <= 0 THEN 1 ELSE 0 END) as loss_count
        FROM copy_trades
        WHERE follower_address = ?
          AND status = 'filled'
          AND date(created_at / 1000, 'unixepoch') >= ?
    """, ("F_MIXED", cutoff)) as cur:
        row = dict(await cur.fetchone())

    assert abs(float(row["total_pnl"]) - 110.0) < 1e-6, f"total_pnl should be 110, got {row['total_pnl']}"
    assert int(row["total_trades"]) == 4
    assert int(row["win_count"] or 0) == 2
    assert int(row["loss_count"] or 0) == 2
    win_rate = 2 / 4
    assert abs(win_rate - 0.5) < 1e-6


# ── TC-PNL-003: 2명 트레이더 → 각각 PnL 분리 ────────────────────────────────

@pytest.mark.asyncio
async def test_pnl_by_trader(db):
    await add_trader(db, "TR_A")
    await add_trader(db, "TR_B")
    await add_follower(db, "F_BYTRADER", "TR_A")

    # TR_A: +200, TR_B: -50
    await record_copy_trade(db, _make_trade("F_BYTRADER", "TR_A", pnl=200.0))
    await record_copy_trade(db, _make_trade("F_BYTRADER", "TR_A", pnl=50.0))
    await record_copy_trade(db, _make_trade("F_BYTRADER", "TR_B", pnl=-50.0))

    async with db.execute("""
        SELECT
            trader_address,
            COALESCE(SUM(CASE WHEN pnl IS NOT NULL THEN pnl ELSE 0 END), 0) as total_pnl,
            COUNT(*) as trades
        FROM copy_trades
        WHERE follower_address = ? AND status = 'filled'
        GROUP BY trader_address
    """, ("F_BYTRADER",)) as cur:
        rows = {dict(r)["trader_address"]: dict(r) for r in await cur.fetchall()}

    assert "TR_A" in rows and "TR_B" in rows
    assert abs(float(rows["TR_A"]["total_pnl"]) - 250.0) < 1e-6
    assert abs(float(rows["TR_B"]["total_pnl"]) - (-50.0)) < 1e-6
    assert int(rows["TR_A"]["trades"]) == 2
    assert int(rows["TR_B"]["trades"]) == 1


# ── TC-PNL-004: snapshot 후 이력 조회 ────────────────────────────────────────

@pytest.mark.asyncio
async def test_pnl_history():
    """file-based DB로 snapshot → history 조회 검증 (스키마 통일)"""
    import tempfile, os as _os, aiosqlite

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp_path = f.name

    try:
        # DB 모델로 초기화 (database.py 스키마 사용)
        conn = await init_db(tmp_path)
        await add_trader(conn, "T_HISTORY")
        await add_follower(conn, "F_HISTORY", "T_HISTORY")

        today = __import__("datetime").datetime.utcnow().strftime("%Y-%m-%d")
        trade = _make_trade("F_HISTORY", "T_HISTORY", pnl=75.0)
        await record_copy_trade(conn, trade)
        await conn.close()

        # DB 모델 클래스로 snapshot 실행 (같은 파일)
        m = DB(tmp_path)
        await m.init()   # 신규 테이블(follower_pnl_daily 등)만 추가
        await m.snapshot_follower_pnl("F_HISTORY", date=today)

        # 이력 조회
        history = await m.get_follower_pnl_history("F_HISTORY", days=30)
        assert len(history) >= 1, f"이력이 비어있음: {history}"
        today_entry = next((h for h in history if h["date"] == today), None)
        assert today_entry is not None, f"오늘({today}) 기록이 없음: {history}"
        assert abs(float(today_entry["realized_pnl"]) - 75.0) < 1e-6
    finally:
        _os.unlink(tmp_path)


# ── TC-PNL-005: snapshot_follower_pnl() 후 DB 확인 ──────────────────────────

@pytest.mark.asyncio
async def test_snapshot_follower_pnl():
    """file-based DB로 snapshot 저장 및 검증 (스키마 통일)"""
    import tempfile, os as _os, aiosqlite
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp_path = f.name

    try:
        # database.py 스키마로 초기화 후 트레이더/팔로워/거래 삽입
        conn = await init_db(tmp_path)
        await add_trader(conn, "T_SNAP")
        await add_follower(conn, "F_SNAP", "T_SNAP")

        today = __import__("datetime").datetime.utcnow().strftime("%Y-%m-%d")
        for pnl in [30.0, -10.0, 20.0]:
            await record_copy_trade(conn, _make_trade("F_SNAP", "T_SNAP", pnl=pnl))
        await conn.close()

        # DB 모델 클래스로 신규 테이블 추가 후 snapshot
        m = DB(tmp_path)
        await m.init()
        await m.snapshot_follower_pnl("F_SNAP", date=today)

        # DB 직접 확인
        async with aiosqlite.connect(tmp_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM follower_pnl_daily WHERE follower_address=? AND date=?",
                ("F_SNAP", today)
            ) as cur:
                row = await cur.fetchone()

        assert row is not None, "snapshot이 저장되지 않음"
        r = dict(row)
        assert abs(float(r["realized_pnl"]) - 40.0) < 1e-6, f"expected 40.0, got {r['realized_pnl']}"
        assert int(r["trade_count"]) == 3
        assert int(r["win_count"]) == 2
        assert int(r["loss_count"]) == 1
    finally:
        _os.unlink(tmp_path)


# ── TC-PNL-006: limit/offset 페이지네이션 ────────────────────────────────────

@pytest.mark.asyncio
async def test_pnl_trades_pagination(db):
    await add_trader(db, "T_PAGE")
    await add_follower(db, "F_PAGE", "T_PAGE")

    # 10건 삽입
    for i in range(10):
        await record_copy_trade(db, _make_trade(
            "F_PAGE", "T_PAGE",
            pnl=float(i),
            ts=int(time.time() * 1000) + i * 1000,
        ))

    # limit=3, offset=0
    async with db.execute(
        "SELECT * FROM copy_trades WHERE follower_address=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
        ("F_PAGE", 3, 0)
    ) as cur:
        page1 = await cur.fetchall()

    # limit=3, offset=3
    async with db.execute(
        "SELECT * FROM copy_trades WHERE follower_address=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
        ("F_PAGE", 3, 3)
    ) as cur:
        page2 = await cur.fetchall()

    assert len(page1) == 3
    assert len(page2) == 3

    ids1 = {dict(r)["id"] for r in page1}
    ids2 = {dict(r)["id"] for r in page2}
    assert ids1.isdisjoint(ids2), "페이지 간 중복 항목이 있어서는 안 됨"

    # 전체 카운트
    async with db.execute(
        "SELECT COUNT(*) as cnt FROM copy_trades WHERE follower_address=?",
        ("F_PAGE",)
    ) as cur:
        total = dict(await cur.fetchone())["cnt"]

    assert total == 10
