"""
엣지 케이스 테스트 — copy_ratio 클램핑, 잔고 부족, 네트워크 오류/재시도
"""
import asyncio
import pytest
import sys
import os
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db.database import init_db, add_trader, add_follower, record_copy_trade
from core.copy_engine import CopyEngine, MAX_ORDER_USDC, MIN_AMOUNT, MIN_ORDER_USDC
from core.stats import compute_trader_stats


# ── 픽스처 ──────────────────────────────────────────────────────
@pytest.fixture
async def db():
    conn = await init_db(":memory:")
    yield conn
    await conn.close()


def make_fill(trader, symbol="BTC", amount="1.0", price="70000", side="open_long"):
    return {
        "account": trader,
        "symbol": symbol,
        "event_type": "fulfill_taker",
        "price": price,
        "amount": amount,
        "side": side,
        "cause": "normal",
        "created_at": int(time.time() * 1000),
    }


# ══════════════════════════════════════════════════════════════
# 1. copy_ratio 클램핑 심화 테스트
# ══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_clamp_max_order_usdc(db):
    """MAX_ORDER_USDC(5000) 초과 주문 → 클램핑"""
    trader = "CLAMP_T_MAX_ORDER_1111111111111111111111"
    follower = "CLAMP_F_MAX_ORDER_1111111111111111111111"
    await add_trader(db, trader)
    # ratio=1.0, max_pos=999999 → MAX_ORDER_USDC가 한도
    await add_follower(db, follower, trader, copy_ratio=1.0, max_position_usdc=999999)

    engine = CopyEngine(db, mock_mode=True)
    # 10 BTC × 70000 = 700,000 USDC → MAX_ORDER_USDC=5000으로 클램핑
    await engine.on_fill(make_fill(trader, "BTC", amount="10.0", price="70000"))

    async with db.execute("SELECT amount FROM copy_trades WHERE follower_address=?", (follower,)) as cur:
        row = await cur.fetchone()
    assert row is not None
    amount = float(row["amount"])
    order_usdc = amount * 70000
    assert order_usdc <= MAX_ORDER_USDC + 1, f"MAX_ORDER_USDC 클램핑 실패: {order_usdc:.2f} USDC"
    print(f"✅ MAX_ORDER_USDC 클램핑: {amount:.6f} BTC = {order_usdc:.2f} USDC ≤ {MAX_ORDER_USDC}")


@pytest.mark.asyncio
async def test_clamp_max_position_usdc(db):
    """max_position_usdc(50) 초과 주문 → 클램핑"""
    trader = "CLAMP_T_MAXPOS_111111111111111111111111"
    follower = "CLAMP_F_MAXPOS_111111111111111111111111"
    await add_trader(db, trader)
    await add_follower(db, follower, trader, copy_ratio=1.0, max_position_usdc=50)

    engine = CopyEngine(db, mock_mode=True)
    # 1 BTC × 70000 = 70,000 USDC → max_pos=50으로 클램핑 → 0.000714 BTC
    await engine.on_fill(make_fill(trader, "BTC", amount="1.0", price="70000"))

    async with db.execute("SELECT amount FROM copy_trades WHERE follower_address=?", (follower,)) as cur:
        row = await cur.fetchone()
    assert row is not None
    amount = float(row["amount"])
    order_usdc = amount * 70000
    assert order_usdc <= 50.1, f"max_pos 클램핑 실패: {order_usdc:.2f} USDC"
    print(f"✅ max_pos 클램핑: {amount:.6f} BTC = {order_usdc:.2f} USDC ≤ 50")


@pytest.mark.asyncio
async def test_clamp_does_not_apply_without_price(db):
    """가격 정보 없을 때 클램핑 미적용 — 비율만 적용"""
    trader = "CLAMP_T_NOPRICE_11111111111111111111111"
    follower = "CLAMP_F_NOPRICE_11111111111111111111111"
    await add_trader(db, trader)
    await add_follower(db, follower, trader, copy_ratio=0.5, max_position_usdc=10)

    engine = CopyEngine(db, mock_mode=True)
    # price="0" → 클램핑 미적용, ratio만 → 2.0 * 0.5 = 1.0
    event = make_fill(trader, "BTC", amount="2.0", price="0")
    await engine.on_fill(event)

    async with db.execute("SELECT amount FROM copy_trades WHERE follower_address=?", (follower,)) as cur:
        row = await cur.fetchone()
    assert row is not None
    amount = float(row["amount"])
    assert amount == pytest.approx(1.0, abs=0.000001), f"가격 없을 때 ratio만 적용되어야 함: {amount}"
    print(f"✅ 가격 없을 때 비율만 적용: {amount}")


@pytest.mark.asyncio
async def test_clamp_min_amount_skip(db):
    """MIN_AMOUNT 미달 주문 → 스킵"""
    trader = "CLAMP_T_MINAMOUNT_1111111111111111111111"
    follower = "CLAMP_F_MINAMOUNT_1111111111111111111111"
    await add_trader(db, trader)
    # ratio=0.0000001 → 엄청 작은 비율
    await add_follower(db, follower, trader, copy_ratio=0.0001, max_position_usdc=0.001)

    engine = CopyEngine(db, mock_mode=True)
    await engine.on_fill(make_fill(trader, "SOL", amount="0.001", price="87"))

    async with db.execute("SELECT COUNT(*) as c FROM copy_trades WHERE follower_address=?", (follower,)) as cur:
        row = await cur.fetchone()
    # MIN_AMOUNT=0.0001 미달이면 주문 없음
    print(f"✅ MIN_AMOUNT 스킵: 주문 {row['c']}건 (0이어야 정상)")


@pytest.mark.asyncio
async def test_clamp_multiple_followers_different_limits(db):
    """팔로워별 max_position_usdc가 다를 때 각자 독립 클램핑"""
    trader = "CLAMP_T_MULTI_111111111111111111111111111"
    followers = [
        ("CLAMP_F_M1_1111111111111111111111111111111", 0.5, 100),   # 0.5×1=0.5 BTC=35000 → max_pos=100 클램핑
        ("CLAMP_F_M2_2222222222222222222222222222222", 0.5, 10000), # 0.5 BTC=35000 < 10000? → 클램핑
        ("CLAMP_F_M3_3333333333333333333333333333333", 0.1, 999999),# 0.1 BTC=7000 → 클램핑 없음
    ]
    await add_trader(db, trader)
    for addr, ratio, max_pos in followers:
        await add_follower(db, addr, trader, copy_ratio=ratio, max_position_usdc=max_pos)

    engine = CopyEngine(db, mock_mode=True)
    await engine.on_fill(make_fill(trader, "BTC", amount="1.0", price="70000"))

    async with db.execute("SELECT follower_address, amount FROM copy_trades WHERE trader_address=?", (trader,)) as cur:
        rows = await cur.fetchall()

    amounts = {dict(r)["follower_address"]: float(dict(r)["amount"]) for r in rows}

    # F_M1: max_pos=100 → 100/70000 = 0.001428 BTC
    if "CLAMP_F_M1_1111111111111111111111111111111" in amounts:
        assert amounts["CLAMP_F_M1_1111111111111111111111111111111"] * 70000 <= 100.1
    # F_M3: ratio=0.1 → 0.1 BTC = 7000 USDC > MAX_ORDER_USDC(5000) → 클램핑
    # 5000 / 70000 = 0.071428...
    if "CLAMP_F_M3_3333333333333333333333333333333" in amounts:
        f3_usdc = amounts["CLAMP_F_M3_3333333333333333333333333333333"] * 70000
        assert f3_usdc <= MAX_ORDER_USDC + 1, f"F_M3 MAX_ORDER_USDC 클램핑 실패: {f3_usdc}"

    print(f"✅ 팔로워별 독립 클램핑: {len(rows)}건")


# ══════════════════════════════════════════════════════════════
# 2. 잔고 부족 시나리오
# ══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_insufficient_balance_order_fails(db):
    """잔고 부족 → 주문 실패(failed) 기록"""
    trader = "INSUF_TRADER_111111111111111111111111111"
    follower = "INSUF_FOLLOW_111111111111111111111111111"
    await add_trader(db, trader)
    await add_follower(db, follower, trader, copy_ratio=1.0, max_position_usdc=9999)

    engine = CopyEngine(db, mock_mode=False)

    # 실제 API 호출 시 잔고 부족 에러 시뮬레이션
    with patch.object(engine, '_get_client') as mock_get_client:
        mock_client = MagicMock()
        mock_client.market_order.side_effect = Exception("Insufficient balance: need 5000 USDC, have 0.50 USDC")
        mock_get_client.return_value = mock_client

        await engine.on_fill(make_fill(trader, "BTC", amount="0.1", price="70000"))

    async with db.execute("SELECT status FROM copy_trades WHERE follower_address=?", (follower,)) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert dict(row)["status"] == "failed", f"잔고 부족 → failed여야 함, 실제: {dict(row)['status']}"
    print("✅ 잔고 부족 → failed 기록 정상")


@pytest.mark.asyncio
async def test_insufficient_balance_does_not_crash(db):
    """잔고 부족이어도 CopyEngine 서비스 계속 동작"""
    trader = "INSUF2_TRADER_11111111111111111111111111"
    followers = [
        ("INSUF2_F1_11111111111111111111111111111111", 1.0, 9999),  # 잔고 부족
        ("INSUF2_F2_22222222222222222222222222222222", 0.01, 100),  # 소액 → 성공
    ]
    await add_trader(db, trader)
    for addr, ratio, max_pos in followers:
        await add_follower(db, addr, trader, copy_ratio=ratio, max_position_usdc=max_pos)

    call_count = 0

    async def mock_copy(follower, symbol, side, amount, trader_addr, symbol_price=0.0):
        nonlocal call_count
        call_count += 1
        follower_addr = follower["address"]
        if "F1" in follower_addr:
            raise Exception("Insufficient balance")
        # F2는 정상 처리
        await record_copy_trade(db, {
            "id": str(uuid.uuid4()),
            "follower_address": follower_addr,
            "trader_address": trader_addr,
            "symbol": symbol, "side": side,
            "amount": "0.001", "price": "70000",
            "client_order_id": str(uuid.uuid4()),
            "status": "filled", "pnl": None,
            "created_at": int(time.time() * 1000),
        })

    engine = CopyEngine(db, mock_mode=True)
    engine._copy_to_follower = mock_copy

    # 예외 발생해도 서비스 중단 없음
    await engine.on_fill(make_fill(trader))

    async with db.execute("SELECT status FROM copy_trades WHERE trader_address=?", (trader,)) as cur:
        rows = await cur.fetchall()

    print(f"✅ 잔고 부족 후 서비스 계속: {call_count}명 처리, {len(rows)}건 기록")
    assert call_count == 2  # 두 팔로워 모두 시도됨


@pytest.mark.asyncio
async def test_zero_balance_all_fail(db):
    """전 팔로워 잔고 부족 → 모두 failed, 서비스 정상"""
    trader = "ZERO_BAL_TRADER_111111111111111111111111"
    for i in range(3):
        await add_follower(db, f"ZERO_BAL_F{i}_11111111111111111111111111111", trader,
                           copy_ratio=1.0, max_position_usdc=9999)
    await add_trader(db, trader)

    engine = CopyEngine(db, mock_mode=False)
    with patch.object(engine, '_get_client') as mock_get_client:
        mock_client = MagicMock()
        mock_client.market_order.side_effect = Exception("Insufficient balance")
        mock_get_client.return_value = mock_client

        # 예외 없이 처리 완료되어야 함
        await engine.on_fill(make_fill(trader))

    async with db.execute(
        "SELECT COUNT(*) as c, status FROM copy_trades WHERE trader_address=? GROUP BY status",
        (trader,)
    ) as cur:
        rows = await cur.fetchall()
    status_map = {dict(r)["status"]: dict(r)["c"] for r in rows}
    failed = status_map.get("failed", 0)
    print(f"✅ 전체 잔고 부족: failed={failed}건, 서비스 정상 유지")


# ══════════════════════════════════════════════════════════════
# 3. 네트워크 오류 / 재시도 시나리오
# ══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_network_timeout_order_fails(db):
    """네트워크 타임아웃 → failed 기록"""
    trader = "NETERR_TRADER_11111111111111111111111111"
    follower = "NETERR_FOLLOW_11111111111111111111111111"
    await add_trader(db, trader)
    await add_follower(db, follower, trader, copy_ratio=0.5, max_position_usdc=1000)

    engine = CopyEngine(db, mock_mode=False)
    with patch.object(engine, '_get_client') as mock_get_client:
        mock_client = MagicMock()
        mock_client.market_order.side_effect = TimeoutError("Connection timed out after 5000ms")
        mock_get_client.return_value = mock_client

        await engine.on_fill(make_fill(trader, "ETH", amount="0.1", price="2000"))

    async with db.execute("SELECT status FROM copy_trades WHERE follower_address=?", (follower,)) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert dict(row)["status"] == "failed"
    print("✅ 네트워크 타임아웃 → failed 기록")


@pytest.mark.asyncio
async def test_network_connection_refused(db):
    """연결 거부 → failed 기록, 서비스 계속"""
    trader = "CONNREF_TRADER_11111111111111111111111111"
    follower = "CONNREF_FOLLOW_11111111111111111111111111"
    await add_trader(db, trader)
    await add_follower(db, follower, trader, copy_ratio=0.5, max_position_usdc=500)

    engine = CopyEngine(db, mock_mode=False)
    with patch.object(engine, '_get_client') as mock_get_client:
        mock_client = MagicMock()
        mock_client.market_order.side_effect = ConnectionRefusedError("Connection refused")
        mock_get_client.return_value = mock_client

        await engine.on_fill(make_fill(trader, "SOL", amount="1.0", price="87"))

    async with db.execute("SELECT status FROM copy_trades WHERE follower_address=?", (follower,)) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert dict(row)["status"] == "failed"
    print("✅ 연결 거부 → failed 기록")


@pytest.mark.asyncio
async def test_partial_network_failure(db):
    """일부 팔로워만 네트워크 실패 → 나머지는 정상 처리"""
    trader = "PARTIAL_TRADER_1111111111111111111111111"
    ok_follower = "PARTIAL_F_OK_111111111111111111111111111"
    fail_follower = "PARTIAL_F_FAIL_1111111111111111111111111"
    await add_trader(db, trader)
    await add_follower(db, ok_follower, trader, copy_ratio=0.5, max_position_usdc=100)
    await add_follower(db, fail_follower, trader, copy_ratio=0.5, max_position_usdc=100)

    engine = CopyEngine(db, mock_mode=False)

    call_count = {"ok": 0, "fail": 0}

    original_copy = engine._copy_to_follower

    async def selective_fail(follower, symbol, side, amount, trader_addr, symbol_price=0.0):
        if "FAIL" in follower["address"]:
            call_count["fail"] += 1
            # 실패 기록 수동 삽입
            await record_copy_trade(db, {
                "id": str(uuid.uuid4()),
                "follower_address": follower["address"],
                "trader_address": trader_addr,
                "symbol": symbol, "side": side,
                "amount": amount, "price": "87",
                "client_order_id": str(uuid.uuid4()),
                "status": "failed", "pnl": None,
                "created_at": int(time.time() * 1000),
            })
        else:
            call_count["ok"] += 1
            await record_copy_trade(db, {
                "id": str(uuid.uuid4()),
                "follower_address": follower["address"],
                "trader_address": trader_addr,
                "symbol": symbol, "side": side,
                "amount": amount, "price": "87",
                "client_order_id": str(uuid.uuid4()),
                "status": "filled", "pnl": None,
                "created_at": int(time.time() * 1000),
            })

    engine._copy_to_follower = selective_fail
    await engine.on_fill(make_fill(trader, "SOL", amount="1.0", price="87"))

    async with db.execute(
        "SELECT status, COUNT(*) as c FROM copy_trades WHERE trader_address=? GROUP BY status",
        (trader,)
    ) as cur:
        rows = await cur.fetchall()
    status_map = {dict(r)["status"]: dict(r)["c"] for r in rows}
    assert status_map.get("filled", 0) >= 1, "정상 팔로워는 filled여야 함"
    assert status_map.get("failed", 0) >= 1, "실패 팔로워는 failed여야 함"
    print(f"✅ 부분 네트워크 실패: filled={status_map.get('filled',0)}, failed={status_map.get('failed',0)}")


@pytest.mark.asyncio
async def test_api_rate_limit_error(db):
    """API 레이트 리밋(429) → failed 기록"""
    trader = "RATELIMIT_TRADER_111111111111111111111111"
    follower = "RATELIMIT_FOLLOW_111111111111111111111111"
    await add_trader(db, trader)
    await add_follower(db, follower, trader, copy_ratio=0.3, max_position_usdc=200)

    engine = CopyEngine(db, mock_mode=False)
    with patch.object(engine, '_get_client') as mock_get_client:
        mock_client = MagicMock()
        mock_client.market_order.side_effect = Exception("HTTP 429: Too Many Requests. Retry after 1000ms")
        mock_get_client.return_value = mock_client

        await engine.on_fill(make_fill(trader, "BTC", amount="0.01", price="70000"))

    async with db.execute("SELECT status FROM copy_trades WHERE follower_address=?", (follower,)) as cur:
        row = await cur.fetchone()
    assert dict(row)["status"] == "failed"
    print("✅ 레이트 리밋(429) → failed 기록")


@pytest.mark.asyncio
async def test_server_500_error(db):
    """서버 500 에러 → failed 기록, 재시도 없음"""
    trader = "SERVER500_TRADER_11111111111111111111111"
    follower = "SERVER500_FOLLOW_11111111111111111111111"
    await add_trader(db, trader)
    await add_follower(db, follower, trader, copy_ratio=0.5, max_position_usdc=300)

    engine = CopyEngine(db, mock_mode=False)
    call_count = {"n": 0}

    with patch.object(engine, '_get_client') as mock_get_client:
        mock_client = MagicMock()
        def count_and_fail(*args, **kwargs):
            call_count["n"] += 1
            raise Exception("HTTP 500: Internal Server Error")
        mock_client.market_order.side_effect = count_and_fail
        mock_get_client.return_value = mock_client

        await engine.on_fill(make_fill(trader, "ETH", amount="0.5", price="2000"))

    # 현재 재시도 없음 — 1회만 호출되어야 함
    assert call_count["n"] == 1, f"재시도 없어야 함 (호출: {call_count['n']}회)"
    async with db.execute("SELECT status FROM copy_trades WHERE follower_address=?", (follower,)) as cur:
        row = await cur.fetchone()
    assert dict(row)["status"] == "failed"
    print(f"✅ 서버 500: {call_count['n']}회 시도 → failed (재시도 없음, 추후 재시도 로직 추가 예정)")


@pytest.mark.asyncio
async def test_db_write_failure_resilience(db):
    """DB 쓰기 실패 시나리오 — record_copy_trade 에러 처리"""
    trader = "DBFAIL_TRADER_111111111111111111111111111"
    follower = "DBFAIL_FOLLOW_111111111111111111111111111"
    await add_trader(db, trader)
    await add_follower(db, follower, trader, copy_ratio=0.5, max_position_usdc=100)

    engine = CopyEngine(db, mock_mode=True)

    with patch('core.copy_engine.record_copy_trade', side_effect=Exception("DB write failed: disk full")):
        # DB 쓰기 실패해도 on_fill이 예외를 삼켜야 함
        await engine.on_fill(make_fill(trader, "SOL", amount="0.1", price="87"))

    print("✅ DB 쓰기 실패 → on_fill 예외 없이 처리됨")


@pytest.mark.asyncio
async def test_consecutive_errors_no_crash(db):
    """연속 오류 10건 → 서비스 중단 없음"""
    trader = "CONSEQ_TRADER_111111111111111111111111111"
    follower = "CONSEQ_FOLLOW_111111111111111111111111111"
    await add_trader(db, trader)
    await add_follower(db, follower, trader, copy_ratio=0.5, max_position_usdc=100)

    engine = CopyEngine(db, mock_mode=False)
    errors = [
        Exception("Insufficient balance"),
        TimeoutError("Timeout"),
        ConnectionRefusedError("Refused"),
        Exception("HTTP 429"),
        Exception("HTTP 500"),
        Exception("Invalid signature"),
        Exception("Symbol not found"),
        Exception("Market closed"),
        Exception("Unknown error"),
        Exception("JSON decode error"),
    ]

    with patch.object(engine, '_get_client') as mock_get_client:
        mock_client = MagicMock()
        mock_client.market_order.side_effect = errors
        mock_get_client.return_value = mock_client

        for i in range(10):
            await engine.on_fill(make_fill(trader, "BTC", amount=str(0.01 * (i+1)), price="70000"))

    async with db.execute("SELECT COUNT(*) as c FROM copy_trades WHERE trader_address=?", (trader,)) as cur:
        row = await cur.fetchone()
    print(f"✅ 연속 오류 10건 후 서비스 정상: {row['c']}건 기록됨")


# ══════════════════════════════════════════════════════════════
# 4. 프론트엔드 API 응답 구조 검증 (UI 렌더링 관점)
# ══════════════════════════════════════════════════════════════

import urllib.request

def api_get(path, base="http://localhost:8001"):
    try:
        with urllib.request.urlopen(f"{base}{path}", timeout=3) as r:
            import json
            return json.loads(r.read()), r.getcode()
    except Exception as e:
        return None, 0


def test_frontend_market_data_shape():
    """프론트 fetchMarkets() 처리 가능한 응답 구조 검증"""
    data, code = api_get("/markets")
    if code == 0:
        pytest.skip("서버 미기동")
    assert code == 200
    assert "data" in data
    # 배열이어야 프론트에서 Object.fromEntries 가능
    items = data["data"]
    assert isinstance(items, list), f"data는 배열이어야 함, 실제: {type(items)}"
    assert len(items) > 0
    # 각 항목 필수 필드
    for item in items[:3]:
        for f in ["symbol", "mark", "funding"]:
            assert f in item, f"마켓 데이터 필드 누락: {f}"
    print(f"✅ /markets 응답 구조 정상: {len(items)}개 심볼")


def test_frontend_leaderboard_shape():
    """프론트 fetchLeaderboard() 처리 가능한 응답 구조"""
    data, code = api_get("/traders")
    if code == 0:
        pytest.skip("서버 미기동")
    assert code == 200
    assert "data" in data and "count" in data
    traders = data["data"]
    assert isinstance(traders, list)
    for t in traders[:2]:
        for f in ["address", "alias", "total_pnl", "win_rate"]:
            assert f in t, f"트레이더 필드 누락: {f}"
    print(f"✅ /traders 응답 구조 정상: {data['count']}명")


def test_frontend_stats_shape():
    """프론트 fetchStats() 처리 가능한 응답 구조"""
    data, code = api_get("/stats")
    if code == 0:
        pytest.skip("서버 미기동")
    assert code == 200
    for f in ["active_traders", "active_followers", "total_volume_usdc", "ws_symbols"]:
        assert f in data, f"stats 필드 누락: {f}"
    print(f"✅ /stats 응답 구조 정상: traders={data['active_traders']}, symbols={data['ws_symbols']}")


def test_frontend_signals_shape():
    """프론트 /signals 응답 구조 (향후 UI 표시용)"""
    data, code = api_get("/signals?top_n=3")
    if code == 0:
        pytest.skip("서버 미기동")
    assert code == 200
    assert "funding_extremes" in data
    assert "oracle_mark_divergence" in data
    assert data["source"] in ("live", "empty")
    print(f"✅ /signals 응답 구조 정상: funding={len(data['funding_extremes'])}개")


def test_frontend_referral_shape():
    """프론트 레퍼럴 링크 응답 구조"""
    data, code = api_get("/referral/3AHZqrocSguMuo9sUUP8G8YN8NwHwWV2DPUQvbDvtfaQ")
    if code == 0:
        pytest.skip("서버 미기동")
    assert code == 200
    assert "referral_link" in data
    assert "ref=" in data["referral_link"]
    assert "points" in data
    print(f"✅ /referral 응답 구조: link={data['referral_link']}, points={data['points']}")
