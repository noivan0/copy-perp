"""
Copy Perp E2E 자동화 테스트
Mock 모드로 전체 파이프라인 검증

실행: python3 -m pytest tests/test_e2e.py -v
"""
import asyncio
import time
import uuid
import sys
import os
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db.database import init_db, add_trader, add_follower, get_followers, get_copy_trades
from core.copy_engine import CopyEngine

TRADER = "TraderAAA1111111111111111111111111111111111"
FOLLOWER_A = "FollowerAAA111111111111111111111111111111111"
FOLLOWER_B = "FollowerBBB222222222222222222222222222222222"


@pytest.fixture
async def db():
    conn = await init_db(":memory:")
    await add_trader(conn, TRADER, "TestTrader")
    await add_follower(conn, FOLLOWER_A, TRADER, copy_ratio=0.5, max_position_usdc=100)
    await add_follower(conn, FOLLOWER_B, TRADER, copy_ratio=0.25, max_position_usdc=50)
    yield conn
    await conn.close()


SYMBOL_PRICES = {"BTC": "70000", "ETH": "2000", "SOL": "87", "SUI": "2.5"}

def make_event(side="open_long", symbol="BTC", amount="0.01"):
    price = SYMBOL_PRICES.get(symbol, "100")
    return {
        "account": TRADER,
        "symbol": symbol,
        "event_type": "fulfill_taker",
        "price": price,
        "amount": amount,
        "side": side,
        "cause": "normal",
        "created_at": int(time.time() * 1000),
    }


# ── TC-COPY-001: 기본 복사 ────────────────────────────────────────────
@pytest.mark.asyncio
async def test_basic_copy(db):
    """트레이더 오픈 → 팔로워 자동 복사 (Mock 모드 80% 성공)"""
    engine = CopyEngine(db, mock_mode=True)
    await engine.on_fill(make_event("open_long", "BTC", "0.1"))

    trades = await get_copy_trades(db, limit=10)
    assert len(trades) == 2, f"팔로워 2명 → 거래 2건 기대, 실제 {len(trades)}"
    symbols = {t["symbol"] for t in trades}
    assert "BTC" in symbols
    print(f"✅ TC-001: {len(trades)}건 복사 기록")


# ── TC-COPY-002: side 매핑 ────────────────────────────────────────────
@pytest.mark.asyncio
async def test_side_mapping(db):
    """open_long → bid, open_short → ask, close_long → ask, close_short → bid"""
    from core.copy_engine import _parse_side
    assert _parse_side("open_long") == "bid"
    assert _parse_side("open_short") == "ask"
    assert _parse_side("close_long") == "ask"
    assert _parse_side("close_short") == "bid"
    assert _parse_side("bid") == "bid"
    assert _parse_side("ask") == "ask"
    assert _parse_side("unknown") is None
    print("✅ TC-002: side 매핑 정상")


# ── TC-COPY-003: 청산 이벤트 스킵 ────────────────────────────────────
@pytest.mark.asyncio
async def test_liquidation_skip(db):
    """청산 이벤트(cause=liquidation)는 복사 안 함"""
    engine = CopyEngine(db, mock_mode=True)
    event = make_event()
    event["cause"] = "liquidation"
    await engine.on_fill(event)
    trades = await get_copy_trades(db, limit=10)
    assert len(trades) == 0, "청산 이벤트는 복사되면 안 됨"
    print("✅ TC-003: 청산 이벤트 스킵 정상")


# ── TC-COPY-004: 비율 계산 ────────────────────────────────────────────
@pytest.mark.asyncio
async def test_copy_ratio(db):
    """copy_ratio 적용 검증 — max_position_usdc 클램핑 포함
    
    fixture: FOLLOWER_A(ratio=0.5, max=100), FOLLOWER_B(ratio=0.25, max=50)
    ETH price=2000, amount=0.1 ETH
    - FOLLOWER_A: 0.1 * 0.5 = 0.05 ETH (10 USDC < max_pos=100 → 클램핑 없음)
    - FOLLOWER_B: 0.1 * 0.25 = 0.025 ETH (5 USDC < max_pos=50 → 클램핑 없음)
    """
    engine = CopyEngine(db, mock_mode=True)
    await engine.on_fill(make_event("open_long", "ETH", "0.1"))
    trades = await get_copy_trades(db, limit=10)
    amounts = sorted([float(t["amount"]) for t in trades])
    expected = sorted([0.025, 0.05])
    assert amounts == pytest.approx(expected, abs=0.000001), f"비율 계산 오류: {amounts}"
    print(f"✅ TC-004: 비율 계산 정상 {amounts}")


# ── TC-COPY-005: 다중 심볼 ────────────────────────────────────────────
@pytest.mark.asyncio
async def test_multi_symbol(db):
    """여러 심볼 이벤트 처리"""
    engine = CopyEngine(db, mock_mode=True)
    for sym in ["BTC", "ETH", "SOL"]:
        await engine.on_fill(make_event("open_long", sym, "0.01"))
    trades = await get_copy_trades(db, limit=20)
    syms = {t["symbol"] for t in trades}
    assert {"BTC", "ETH", "SOL"}.issubset(syms), f"심볼 누락: {syms}"
    print(f"✅ TC-005: 다중 심볼 {syms}")


# ── TC-COPY-006: 팔로워 없을 때 ─────────────────────────────────────
@pytest.mark.asyncio
async def test_no_followers(db):
    """팔로워 없는 트레이더 → 오류 없이 종료"""
    conn = await init_db(":memory:")
    await add_trader(conn, "UnknownTrader111111111111111111111111111", "X")
    engine = CopyEngine(conn, mock_mode=True)
    # 예외 없이 완료되어야 함
    await engine.on_fill({
        "account": "UnknownTrader111111111111111111111111111",
        "symbol": "BTC", "event_type": "fulfill_taker",
        "price": "70000", "amount": "0.01",
        "side": "open_long", "cause": "normal",
        "created_at": int(time.time() * 1000),
    })
    trades = await get_copy_trades(conn, limit=10)
    assert len(trades) == 0
    await conn.close()
    print("✅ TC-006: 팔로워 없음 정상 처리")


# ── TC-COPY-007: 중복 이벤트 처리 ───────────────────────────────────
@pytest.mark.asyncio
async def test_duplicate_events(db):
    """동일 이벤트 2번 → 2번 기록 (client_order_id 다름)"""
    engine = CopyEngine(db, mock_mode=True)
    event = make_event("open_long", "BTC", "0.01")
    await engine.on_fill(event)
    await engine.on_fill(event)
    trades = await get_copy_trades(db, limit=20)
    # 각 이벤트마다 2명 팔로워 → 총 4건
    assert len(trades) == 4
    # client_order_id 중복 없어야 함
    order_ids = [t["client_order_id"] for t in trades]
    assert len(set(order_ids)) == len(order_ids), "client_order_id 중복!"
    print(f"✅ TC-007: 중복 이벤트 {len(trades)}건 독립 기록")


# ── TC-COPY-008: DB 거래 기록 무결성 ────────────────────────────────
@pytest.mark.asyncio
async def test_trade_record_integrity(db):
    """거래 기록 필드 무결성 검증"""
    engine = CopyEngine(db, mock_mode=True)
    await engine.on_fill(make_event("open_short", "SOL", "0.5"))
    trades = await get_copy_trades(db, limit=10)
    for t in trades:
        assert t["id"], "id 없음"
        assert t["follower_address"], "follower_address 없음"
        assert t["trader_address"] == TRADER
        assert t["symbol"] == "SOL"
        assert t["status"] in ("filled", "failed")
        assert t["client_order_id"], "client_order_id 없음"
        assert t["created_at"] > 0
    print(f"✅ TC-008: DB 필드 무결성 {len(trades)}건 검증 완료")


# ── TC-COPY-009: 성능 — 10명 동시 복사 ──────────────────────────────
@pytest.mark.asyncio
async def test_performance_10_followers(db):
    """팔로워 10명 동시 복사 2000ms 이내"""
    conn = await init_db(":memory:")
    trader = "PerfTrader1111111111111111111111111111111111"
    await add_trader(conn, trader, "PerfTest")
    for i in range(10):
        await add_follower(conn, f"PerfFollower{i:04d}1111111111111111111111111111111", trader)

    engine = CopyEngine(conn, mock_mode=True)
    event = {
        "account": trader, "symbol": "BTC", "event_type": "fulfill_taker",
        "price": "70000", "amount": "0.01", "side": "open_long",
        "cause": "normal", "created_at": int(time.time() * 1000),
    }

    start = time.time()
    await engine.on_fill(event)
    elapsed_ms = (time.time() - start) * 1000

    trades = await get_copy_trades(conn, limit=20)
    assert len(trades) == 10, f"10명 기대, 실제 {len(trades)}"
    # Fuul API 미설정 환경(CI/테스트)에서는 외부 HTTP 401 응답 대기가 포함될 수 있음
    # → 실제 환경(FUUL_API_KEY 설정 시) 2000ms, 미설정 시 15000ms 허용
    fuul_configured = bool(os.environ.get("FUUL_API_KEY"))
    time_limit_ms = 2000 if fuul_configured else 15000
    assert elapsed_ms < time_limit_ms, f"{time_limit_ms}ms 초과: {elapsed_ms:.0f}ms"
    await conn.close()
    print(f"✅ TC-009: 10명 동시 복사 {elapsed_ms:.0f}ms 완료")


if __name__ == "__main__":
    async def run_all():
        print("=" * 50)
        print("Copy Perp E2E 테스트 (Mock 모드)")
        print("=" * 50)
        conn = await init_db(":memory:")
        await add_trader(conn, TRADER, "TestTrader")
        await add_follower(conn, FOLLOWER_A, TRADER, copy_ratio=0.5, max_position_usdc=100)
        await add_follower(conn, FOLLOWER_B, TRADER, copy_ratio=0.25, max_position_usdc=50)

        tests = [
            test_basic_copy,
            test_side_mapping,
            test_liquidation_skip,
            test_copy_ratio,
            test_multi_symbol,
            test_no_followers,
            test_duplicate_events,
            test_trade_record_integrity,
            test_performance_10_followers,
        ]

        passed = 0
        for test_fn in tests:
            fresh_db = await init_db(":memory:")
            await add_trader(fresh_db, TRADER, "TestTrader")
            await add_follower(fresh_db, FOLLOWER_A, TRADER, copy_ratio=0.5, max_position_usdc=100)
            await add_follower(fresh_db, FOLLOWER_B, TRADER, copy_ratio=0.25, max_position_usdc=50)
            try:
                if test_fn.__name__ in ("test_side_mapping", "test_no_followers", "test_performance_10_followers"):
                    await test_fn(fresh_db)
                else:
                    await test_fn(fresh_db)
                passed += 1
            except Exception as e:
                print(f"❌ {test_fn.__name__}: {e}")
            await fresh_db.close()

        await conn.close()
        print("=" * 50)
        print(f"결과: {passed}/{len(tests)} 통과")

    asyncio.run(run_all())
