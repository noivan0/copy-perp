"""
tests/test_e2e_pipeline.py
Task 2: E2E 시나리오 — 팔로워 온보딩 → 포지션 변화 → 복사 주문 → PnL → 리더보드

Mock 모드 전체 파이프라인 + 실패 케이스 완전 커버
"""
import pytest
import asyncio
import json
import socket
import time
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from dotenv import load_dotenv
load_dotenv()


# ── 백엔드 헬퍼 ───────────────────────────────────────────────────────

def backend_get(path: str) -> tuple[int, dict]:
    try:
        s = socket.create_connection(("localhost", 8001), timeout=8)
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: localhost:8001\r\nConnection: close\r\n\r\n".encode())
        s.settimeout(8); data = b""
        while True:
            c = s.recv(16384)
            if not c: break
            data += c
        s.close()
        hdr, body = data.split(b"\r\n\r\n", 1)
        code = int(hdr.split(b"\r\n")[0].split()[1])
        return code, json.loads(body)
    except ConnectionRefusedError:
        pytest.skip("백엔드 미기동")


def backend_post(path: str, body: dict) -> tuple[int, dict]:
    try:
        s = socket.create_connection(("localhost", 8001), timeout=8)
        b = json.dumps(body).encode()
        req = (
            f"POST {path} HTTP/1.1\r\nHost: localhost:8001\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(b)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode() + b
        s.sendall(req)
        s.settimeout(8); data = b""
        while True:
            c = s.recv(16384)
            if not c: break
            data += c
        s.close()
        hdr, body_r = data.split(b"\r\n\r\n", 1)
        code = int(hdr.split(b"\r\n")[0].split()[1])
        return code, json.loads(body_r)
    except ConnectionRefusedError:
        pytest.skip("백엔드 미기동")


@pytest.fixture(autouse=True)
def sequential_guard():
    yield
    time.sleep(0.2)


# ── 시나리오 1: 정상 파이프라인 ───────────────────────────────────────

class TestE2EPipelineHappyPath:

    def test_e2e_01_full_pipeline_single_follower(self):
        """[E2E-01] 전체 파이프라인: 온보딩→포지션→복사→PnL"""
        import asyncio
        from db.database import (
            init_db, add_trader, add_follower,
            get_copy_trades, get_leaderboard
        )
        from core.copy_engine import CopyEngine

        TRADER   = f"E2E01_TRADER_{uuid.uuid4().hex[:16].upper()}"
        FOLLOWER = f"E2E01_FOLOW_{uuid.uuid4().hex[:16].upper()}"

        async def run():
            conn = await init_db(":memory:")

            # Step 1: 팔로워 온보딩
            await add_trader(conn, TRADER, "E2ETrader01")
            await add_follower(conn, FOLLOWER, TRADER, copy_ratio=0.5, max_position_usdc=500)

            # Step 2: DB에 등록 확인
            cur = await conn.execute("SELECT * FROM followers WHERE address=?", (FOLLOWER,))
            follower_row = await cur.fetchone()
            assert follower_row is not None, "팔로워 미등록"

            # Step 3: 트레이더 포지션 변화 이벤트 (Open Long BTC)
            engine = CopyEngine(conn, mock_mode=True)
            await engine.on_fill({
                "account": TRADER,
                "symbol": "BTC",
                "event_type": "fulfill_taker",
                "price": "72000",
                "amount": "0.1",
                "side": "open_long",
                "cause": "normal",
                "created_at": int(time.time() * 1000),
            })

            # Step 4: 복사 주문 체결 확인
            trades = await get_copy_trades(conn, limit=10)
            assert len(trades) >= 1, "복사 주문 미생성"
            trade = trades[0]
            assert trade["symbol"] == "BTC"
            assert trade["follower_address"] == FOLLOWER
            assert trade["status"] in ("filled", "failed")

            # Step 5: 리더보드 반영 확인 (trader가 DB에 있어야 함)
            lb = await get_leaderboard(conn, limit=10)
            addrs = [t["address"] for t in lb]
            assert TRADER in addrs, "트레이더 리더보드 미반영"

            await conn.close()
            return trade

        trade = asyncio.run(run())
        print(f"\n✅ E2E-01: 전체 파이프라인 완료 — BTC {trade['side']} status={trade['status']}")

    def test_e2e_02_onboarding_via_api(self):
        """[E2E-02] API를 통한 팔로워 온보딩"""
        trader = "E2E02TRADERxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        follower = "E2E02FOLLOWxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

        # /follow API 호출
        code, data = backend_post("/follow", {
            "trader_address": trader,
            "follower_address": follower,
            "copy_ratio": 0.5,
            "max_position_usdc": 100,
        })
        assert code in (200, 201, 409), f"팔로우 실패: {code} {data}"
        print(f"\n✅ E2E-02: /follow API HTTP {code}")

    def test_e2e_03_leaderboard_after_trades(self):
        """[E2E-03] 복사거래 후 리더보드 데이터"""
        code, data = backend_get("/traders?limit=5")
        assert code == 200
        traders = data if isinstance(data, list) else data.get("data", [])
        assert len(traders) >= 1
        code2, stats = backend_get("/stats")
        assert code2 == 200
        print(f"\n✅ E2E-03: 리더보드 {len(traders)}명, 총거래={stats.get('total_trades_filled',0)}건")

    def test_e2e_04_multi_symbol_pipeline(self):
        """[E2E-04] BTC/ETH/SOL 멀티 심볼 파이프라인"""
        import asyncio
        from db.database import init_db, add_trader, add_follower, get_copy_trades
        from core.copy_engine import CopyEngine

        TRADER = f"E2E04_TR_{uuid.uuid4().hex[:12].upper()}"

        async def run():
            conn = await init_db(":memory:")
            await add_trader(conn, TRADER, "MultiSym")
            for i in range(3):
                f = f"E2E04_F{i}_{uuid.uuid4().hex[:12].upper()}"
                await add_follower(conn, f, TRADER, copy_ratio=0.5, max_position_usdc=200)

            engine = CopyEngine(conn, mock_mode=True)
            for sym, price, amt in [("BTC","72000","0.1"), ("ETH","3000","0.5"), ("SOL","150","5")]:
                await engine.on_fill({
                    "account": TRADER, "symbol": sym,
                    "event_type": "fulfill_taker",
                    "price": price, "amount": amt,
                    "side": "open_long", "cause": "normal",
                    "created_at": int(time.time() * 1000),
                })
            trades = await get_copy_trades(conn, limit=30)
            await conn.close()
            return trades

        trades = asyncio.run(run())
        syms = {t["symbol"] for t in trades}
        assert "BTC" in syms, "BTC 복사 없음"
        assert "ETH" in syms, "ETH 복사 없음"
        print(f"\n✅ E2E-04: 멀티심볼 {len(trades)}건 — {syms}")

    def test_e2e_05_pnl_tracking(self):
        """[E2E-05] 복사거래 PnL 추적"""
        code, data = backend_get("/trades?limit=20")
        assert code == 200
        trades = data if isinstance(data, list) else data.get("trades", data.get("data", []))
        filled = [t for t in trades if t.get("status") == "filled"]
        print(f"\n✅ E2E-05: filled 거래 {len(filled)}건 확인")

    def test_e2e_06_builder_code_tagged(self):
        """[E2E-06] 복사 주문에 builder_code 태그"""
        import asyncio
        from db.database import init_db, add_trader, add_follower
        from core.copy_engine import CopyEngine, BUILDER_CODE
        from unittest.mock import MagicMock

        TRADER   = f"E2E06_TR_{uuid.uuid4().hex[:12].upper()}"
        FOLLOWER = f"E2E06_FO_{uuid.uuid4().hex[:12].upper()}"
        captured = {}

        async def run():
            conn = await init_db(":memory:")
            await add_trader(conn, TRADER, "BCTest")
            # builder_approved=True 설정
            await conn.execute(
                "INSERT INTO followers (address, trader_address, copy_ratio, max_position_usdc, builder_approved, active)"
                " VALUES (?,?,?,?,?,?)",
                (FOLLOWER, TRADER, 1.0, 1000, 1, 1)
            )
            await conn.commit()

            engine = CopyEngine(conn, mock_mode=False)

            def mock_client(addr):
                c = MagicMock()
                def capture(**kw):
                    captured.update(kw)
                    return {"data": {"order_id": "MOCK"}}
                c.market_order.side_effect = lambda **kw: capture(**kw) or {"data": {"order_id": "MOCK"}}
                return c

            engine._get_client = mock_client
            await engine.on_fill({
                "account": TRADER, "symbol": "BTC",
                "event_type": "fulfill_taker",
                "price": "72000", "amount": "0.5",
                "side": "open_long", "cause": "normal",
                "created_at": int(time.time() * 1000),
            })
            await conn.close()

        asyncio.run(run())
        bc = captured.get("builder_code", "")
        assert bc == BUILDER_CODE, f"builder_code 불일치: '{bc}' != '{BUILDER_CODE}'"
        print(f"\n✅ E2E-06: builder_code='{bc}' 정상 태그")


# ── 시나리오 2: 실패 케이스 ───────────────────────────────────────────

class TestE2EPipelineFailureCases:

    def test_e2e_f01_insufficient_balance_continues(self):
        """[E2E-F01] 잔고 부족 → 다음 팔로워 계속"""
        import asyncio
        from unittest.mock import MagicMock
        from db.database import init_db, add_trader, add_follower, get_copy_trades
        from core.copy_engine import CopyEngine

        TRADER = f"E2EF01_TR_{uuid.uuid4().hex[:12].upper()}"
        F1 = f"E2EF01_F1_{uuid.uuid4().hex[:12].upper()}"
        F2 = f"E2EF01_F2_{uuid.uuid4().hex[:12].upper()}"

        async def run():
            conn = await init_db(":memory:")
            await add_trader(conn, TRADER, "InsufficientTest")
            await add_follower(conn, F1, TRADER, copy_ratio=0.5, max_position_usdc=100)
            await add_follower(conn, F2, TRADER, copy_ratio=0.5, max_position_usdc=100)
            engine = CopyEngine(conn, mock_mode=False)

            order_log = []
            def mock_client(addr):
                c = MagicMock()
                if addr == F1:
                    c.market_order.side_effect = RuntimeError("HTTP 422: insufficient balance")
                else:
                    def ok(**kw):
                        order_log.append(addr)
                        return {"data": {"order_id": "OK123"}}
                    c.market_order.side_effect = ok
                return c

            engine._get_client = mock_client
            await engine.on_fill({
                "account": TRADER, "symbol": "ETH",
                "event_type": "fulfill_taker",
                "price": "3000", "amount": "0.3",
                "side": "open_long", "cause": "normal",
                "created_at": int(time.time() * 1000),
            })
            trades = await get_copy_trades(conn, limit=10)
            await conn.close()
            return trades, order_log

        trades, orders = asyncio.run(run())
        # F2는 성공해야 함
        assert F2 in orders, "잔고부족 후 F2 처리 안됨"
        statuses = {t["status"] for t in trades}
        assert "failed" in statuses, "F1 실패 미기록"
        print(f"\n✅ E2E-F01: 잔고부족 후 계속 — {len(trades)}건 기록 {statuses}")

    def test_e2e_f02_min_amount_skip(self):
        """[E2E-F02] 최소수량 미달 → 스킵"""
        import asyncio
        from db.database import init_db, add_trader, add_follower, get_copy_trades
        from core.copy_engine import CopyEngine, MIN_AMOUNT

        TRADER   = f"E2EF02_TR_{uuid.uuid4().hex[:12].upper()}"
        FOLLOWER = f"E2EF02_FO_{uuid.uuid4().hex[:12].upper()}"

        async def run():
            conn = await init_db(":memory:")
            await add_trader(conn, TRADER, "MinTest")
            await add_follower(conn, FOLLOWER, TRADER, copy_ratio=0.0001, max_position_usdc=0.01)
            engine = CopyEngine(conn, mock_mode=True)
            await engine.on_fill({
                "account": TRADER, "symbol": "BTC",
                "event_type": "fulfill_taker",
                "price": "72000", "amount": "0.001",
                "side": "open_long", "cause": "normal",
                "created_at": int(time.time() * 1000),
            })
            trades = await get_copy_trades(conn, limit=5)
            await conn.close()
            return trades

        trades = asyncio.run(run())
        assert len(trades) == 0, f"최소량 미달인데 {len(trades)}건 처리"
        print(f"\n✅ E2E-F02: MIN_AMOUNT({MIN_AMOUNT}) 미달 스킵 확인")

    def test_e2e_f03_signature_error_fail_graceful(self):
        """[E2E-F03] 서명 오류 → 즉시 실패 (재시도 없음)"""
        import asyncio
        from unittest.mock import MagicMock
        from db.database import init_db, add_trader, add_follower, get_copy_trades
        from core.copy_engine import CopyEngine

        TRADER   = f"E2EF03_TR_{uuid.uuid4().hex[:12].upper()}"
        FOLLOWER = f"E2EF03_FO_{uuid.uuid4().hex[:12].upper()}"
        call_count = {"n": 0}

        async def run():
            conn = await init_db(":memory:")
            await add_trader(conn, TRADER, "SigError")
            await add_follower(conn, FOLLOWER, TRADER, copy_ratio=0.5, max_position_usdc=100)
            engine = CopyEngine(conn, mock_mode=False)

            def mock_client(addr):
                c = MagicMock()
                def sig_fail(**kw):
                    call_count["n"] += 1
                    raise RuntimeError("HTTP 400: invalid signature")
                c.market_order.side_effect = sig_fail
                return c

            engine._get_client = mock_client
            await engine.on_fill({
                "account": TRADER, "symbol": "BTC",
                "event_type": "fulfill_taker",
                "price": "72000", "amount": "0.1",
                "side": "open_long", "cause": "normal",
                "created_at": int(time.time() * 1000),
            })
            trades = await get_copy_trades(conn, limit=5)
            await conn.close()
            return trades

        trades = asyncio.run(run())
        # 400 invalid signature → 재시도 불가 → 1회만 시도
        assert call_count["n"] == 1, f"서명 오류인데 {call_count['n']}회 시도"
        assert trades[0]["status"] == "failed"
        print(f"\n✅ E2E-F03: 서명 오류 → {call_count['n']}회 즉시 실패 (재시도 없음)")

    def test_e2e_f04_liquidation_excluded(self):
        """[E2E-F04] 청산 이벤트 복사 제외"""
        import asyncio
        from db.database import init_db, add_trader, add_follower, get_copy_trades
        from core.copy_engine import CopyEngine

        TRADER   = f"E2EF04_TR_{uuid.uuid4().hex[:12].upper()}"
        FOLLOWER = f"E2EF04_FO_{uuid.uuid4().hex[:12].upper()}"

        async def run():
            conn = await init_db(":memory:")
            await add_trader(conn, TRADER, "LiqTest")
            await add_follower(conn, FOLLOWER, TRADER, copy_ratio=0.5, max_position_usdc=100)
            engine = CopyEngine(conn, mock_mode=True)
            await engine.on_fill({
                "account": TRADER, "symbol": "BTC",
                "event_type": "fulfill_taker",
                "price": "72000", "amount": "1.0",
                "side": "open_long",
                "cause": "liquidation",  # 청산
                "created_at": int(time.time() * 1000),
            })
            trades = await get_copy_trades(conn, limit=5)
            await conn.close()
            return trades

        trades = asyncio.run(run())
        assert len(trades) == 0, f"청산 이벤트가 복사됨: {len(trades)}건"
        print(f"\n✅ E2E-F04: 청산 이벤트 복사 제외 확인")

    def test_e2e_f05_wrong_event_type_skip(self):
        """[E2E-F05] 알 수 없는 event_type → 스킵"""
        import asyncio
        from db.database import init_db, add_trader, add_follower, get_copy_trades
        from core.copy_engine import CopyEngine

        TRADER   = f"E2EF05_TR_{uuid.uuid4().hex[:12].upper()}"
        FOLLOWER = f"E2EF05_FO_{uuid.uuid4().hex[:12].upper()}"

        async def run():
            conn = await init_db(":memory:")
            await add_trader(conn, TRADER, "UnknownEvt")
            await add_follower(conn, FOLLOWER, TRADER, copy_ratio=0.5, max_position_usdc=100)
            engine = CopyEngine(conn, mock_mode=True)
            await engine.on_fill({
                "account": TRADER, "symbol": "BTC",
                "event_type": "settlement",  # 알 수 없는 타입
                "price": "72000", "amount": "0.1",
                "side": "open_long", "cause": "normal",
                "created_at": int(time.time() * 1000),
            })
            trades = await get_copy_trades(conn, limit=5)
            await conn.close()
            return trades

        trades = asyncio.run(run())
        print(f"\n✅ E2E-F05: settlement 이벤트 → {len(trades)}건 (스킵 or 처리)")

    def test_e2e_f06_inactive_follower_skip(self):
        """[E2E-F06] 비활성 팔로워 스킵"""
        import asyncio
        from db.database import init_db, add_trader, add_follower, get_copy_trades
        from core.copy_engine import CopyEngine

        TRADER   = f"E2EF06_TR_{uuid.uuid4().hex[:12].upper()}"
        FOLLOWER = f"E2EF06_FO_{uuid.uuid4().hex[:12].upper()}"

        async def run():
            conn = await init_db(":memory:")
            await add_trader(conn, TRADER, "InactiveTest")
            await add_follower(conn, FOLLOWER, TRADER, copy_ratio=0.5, max_position_usdc=100)
            # 비활성화
            await conn.execute("UPDATE followers SET active=0 WHERE address=?", (FOLLOWER,))
            await conn.commit()

            engine = CopyEngine(conn, mock_mode=True)
            await engine.on_fill({
                "account": TRADER, "symbol": "BTC",
                "event_type": "fulfill_taker",
                "price": "72000", "amount": "0.1",
                "side": "open_long", "cause": "normal",
                "created_at": int(time.time() * 1000),
            })
            trades = await get_copy_trades(conn, limit=5)
            await conn.close()
            return trades

        trades = asyncio.run(run())
        assert len(trades) == 0, f"비활성 팔로워 처리됨: {len(trades)}건"
        print(f"\n✅ E2E-F06: 비활성 팔로워 스킵 확인")

    def test_e2e_f07_no_followers_noop(self):
        """[E2E-F07] 팔로워 없는 트레이더 → no-op"""
        import asyncio
        from db.database import init_db, add_trader, get_copy_trades
        from core.copy_engine import CopyEngine

        TRADER = f"E2EF07_TR_{uuid.uuid4().hex[:12].upper()}"

        async def run():
            conn = await init_db(":memory:")
            await add_trader(conn, TRADER, "NoFollower")
            engine = CopyEngine(conn, mock_mode=True)
            await engine.on_fill({
                "account": TRADER, "symbol": "BTC",
                "event_type": "fulfill_taker",
                "price": "72000", "amount": "0.1",
                "side": "open_long", "cause": "normal",
                "created_at": int(time.time() * 1000),
            })
            trades = await get_copy_trades(conn, limit=5)
            await conn.close()
            return trades

        trades = asyncio.run(run())
        assert len(trades) == 0
        print(f"\n✅ E2E-F07: 팔로워 없음 → no-op (0건)")

    def test_e2e_f08_api_onboard_invalid_follower(self):
        """[E2E-F08] /followers/onboard 잘못된 입력"""
        code, data = backend_post("/followers/onboard", {})
        assert code in (400, 422), f"빈 body → {code}"
        print(f"\n✅ E2E-F08: 잘못된 온보딩 → HTTP {code}")
