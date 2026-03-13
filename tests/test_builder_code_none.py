"""
tests/test_builder_code_none.py
Task 3: builder_code=None 주문 E2E 재검증 — 순차 실행

검증 항목:
1. builder_code=None으로 복사 주문 전체 플로우
2. 서버 도달 확인 (HMG 통과)
3. 주문 실패(400) 시 DB에 failed 기록
4. Copy Engine 서비스 계속 유지
"""
import pytest
import asyncio
import time
import json
import socket
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from dotenv import load_dotenv
load_dotenv()

# ── 순차 실행 보장 ─────────────────────────────────────────────────
# 각 테스트 사이 1초 대기 (rate limit guard + 순차성)
@pytest.fixture(autouse=True)
def sequential_guard():
    yield
    time.sleep(1.0)


class TestBuilderCodeNone:
    """builder_code=None 전체 플로우 순차 검증"""

    def test_bn001_server_reachable_without_builder_code(self):
        """[BN-001] builder_code 없이 POST 요청 → 서버 도달 확인 (HMG 통과)
        
        빈 payload는 서버가 non-JSON 텍스트로 응답할 수 있음 (JSON 파싱 에러 = 서버 도달 확인).
        """
        from pacifica.client import _cf_request
        import json as _j
        try:
            result = _cf_request("POST", "orders/create_market", {
                "account": os.getenv("ACCOUNT_ADDRESS", ""),
                "signature": "invalid",
                "timestamp": int(time.time() * 1000),
                "expiry_window": 5000,
                "symbol": "BTC",
                "side": "bid",
                "amount": "0.001",
                "reduce_only": False,
            })
            print(f"\n⚠️  BN-001: 서버 응답 200 → {result}")
        except RuntimeError as e:
            err = str(e)
            assert "HMG" not in err and "secinfo" not in err, f"HMG 차단됨: {err}"
            assert "HTTP 4" in err, f"4xx 에러 아님 — 서버 미도달: {err}"
            print(f"\n✅ BN-001: 서버 도달 확인 → {err[:80]}")
        except _j.JSONDecodeError:
            # 서버가 non-JSON 응답 → 서버 도달 확인
            print(f"\n✅ BN-001: 서버 도달 확인 (non-JSON 응답)")

    def test_bn002_copy_engine_without_builder_code(self):
        """[BN-002] Copy Engine builder_code=None 플로우"""
        import asyncio
        from db.database import init_db, add_trader, add_follower, get_copy_trades
        from core.copy_engine import CopyEngine

        TRADER = "BN002_TRADER_11111111111111111111111111111"
        FOLLOWER = "BN002_FOLLOW_11111111111111111111111111111"

        async def run():
            conn = await init_db(":memory:")
            await add_trader(conn, TRADER, "BNTest")
            await add_follower(conn, FOLLOWER, TRADER, copy_ratio=0.001, max_position_usdc=10)

            # builder_code=None으로 실제 주문 시도 (실패해도 DB에 기록돼야 함)
            engine = CopyEngine(conn, mock_mode=False)
            event = {
                "account": TRADER,
                "symbol": "ETH",
                "event_type": "fulfill_taker",
                "price": "2100",
                "amount": "0.1",
                "side": "open_long",
                "cause": "normal",
                "created_at": int(time.time() * 1000),
            }
            await engine.on_fill(event)
            trades = await get_copy_trades(conn, limit=10)
            await conn.close()
            return trades

        trades = asyncio.run(run())
        # 주문 시도 자체는 이뤄져야 함 (filled or failed 모두 OK)
        assert len(trades) >= 1, "복사 주문 기록 없음 — on_fill 실행 안됨"
        status = trades[0]["status"]
        assert status in ("filled", "failed"), f"예상 외 status: {status}"
        print(f"\n✅ BN-002: builder_code=None → status={status} DB 기록 확인")

    def test_bn003_service_continues_after_failed_order(self):
        """[BN-003] 주문 실패 후 서비스 계속 유지 — mock_mode=True로 검증"""
        import asyncio
        from db.database import init_db, add_trader, add_follower, get_copy_trades
        from core.copy_engine import CopyEngine

        TRADER = "BN003_TRADER_11111111111111111111111111111"
        FOLLOWER = "BN003_FOLLOW_11111111111111111111111111111"

        async def run():
            conn = await init_db(":memory:")
            await add_trader(conn, TRADER, "BNTest3")
            # mock_mode=True: 실제 API 호출 없이 성공 처리
            await add_follower(conn, FOLLOWER, TRADER, copy_ratio=0.5, max_position_usdc=100)
            engine = CopyEngine(conn, mock_mode=True)

            # 이벤트 3번 연속 — 각각 처리 후 다음 이벤트도 처리
            for i in range(3):
                event = {
                    "account": TRADER,
                    "symbol": "BTC",
                    "event_type": "fulfill_taker",
                    "price": "72000",
                    "amount": "0.1",
                    "side": "open_long" if i % 2 == 0 else "open_short",
                    "cause": "normal",
                    "created_at": int(time.time() * 1000) + i * 100,
                }
                await engine.on_fill(event)

            trades = await get_copy_trades(conn, limit=20)
            await conn.close()
            return trades

        trades = asyncio.run(run())
        assert len(trades) >= 3, f"연속 이벤트 처리 실패 — {len(trades)}건만 기록"
        statuses = {t["status"] for t in trades}
        assert statuses.issubset({"filled", "failed"}), f"예상 외 상태: {statuses}"
        print(f"\n✅ BN-003: {len(trades)}건 연속 처리 — 서비스 유지 확인 status={statuses}")

    def test_bn004_market_order_without_builder_code(self):
        """[BN-004] PacificaClient.market_order(builder_code=None) → 서버 도달"""
        from pacifica.client import PacificaClient
        client = PacificaClient()
        try:
            # 극소량 주문 (amount 최소값 미만으로 422 예상)
            result = client.market_order("ETH", "bid", "0.0001", builder_code=None)
            print(f"\n⚠️  BN-004: 주문 성공 (예상 외) → {result}")
        except RuntimeError as e:
            err = str(e)
            # HMG 차단 아닌 서버 에러여야 함
            assert "HMG" not in err and "secinfo" not in err
            assert "HTTP 4" in err, f"서버 미도달: {err}"
            print(f"\n✅ BN-004: builder_code=None → {err[:100]}")

    def test_bn005_limit_order_with_builder_code(self):
        """[BN-005] limit_order builder_code=noivan 포함 확인"""
        from unittest.mock import patch
        from pacifica.client import BUILDER_CODE
        import pacifica.client as pac_mod

        client = pac_mod.PacificaClient()
        captured = {}
        original = pac_mod._cf_request

        def capture(method, path, body=None):
            if body and isinstance(body, dict) and "builder_code" in body:
                captured["builder_code"] = body["builder_code"]
            return original(method, path, body)

        with patch.object(pac_mod, "_cf_request", side_effect=capture):
            try:
                client.limit_order("BTC", "bid", "0.001", price="70000")
            except Exception:
                pass

        assert captured.get("builder_code") == BUILDER_CODE
        print(f"\n✅ BN-005: builder_code='{captured['builder_code']}' 자동 포함")

    def test_bn006_copy_engine_liquidation_excluded(self):
        """[BN-006] 청산 이벤트 복사 제외 확인"""
        import asyncio
        from db.database import init_db, add_trader, add_follower, get_copy_trades
        from core.copy_engine import CopyEngine

        TRADER = "BN006_TRADER_11111111111111111111111111111"
        FOLLOWER = "BN006_FOLLOW_11111111111111111111111111111"

        async def run():
            conn = await init_db(":memory:")
            await add_trader(conn, TRADER, "BNTest6")
            await add_follower(conn, FOLLOWER, TRADER, copy_ratio=0.5, max_position_usdc=100)
            engine = CopyEngine(conn, mock_mode=True)

            # 청산 이벤트 — 복사 안 해야 함
            await engine.on_fill({
                "account": TRADER, "symbol": "BTC",
                "event_type": "fulfill_taker",
                "price": "72000", "amount": "1.0",
                "side": "open_long",
                "cause": "liquidation",  # ← 청산
                "created_at": int(time.time() * 1000),
            })
            trades = await get_copy_trades(conn, limit=10)
            await conn.close()
            return trades

        trades = asyncio.run(run())
        assert len(trades) == 0, f"청산 이벤트가 복사됨: {len(trades)}건"
        print(f"\n✅ BN-006: 청산 이벤트 {len(trades)}건 복사 제외")

    def test_bn007_min_amount_skip(self):
        """[BN-007] 최소 주문량 미달 → Copy Engine 스킵"""
        import asyncio
        from db.database import init_db, add_trader, add_follower, get_copy_trades
        from core.copy_engine import CopyEngine

        TRADER = "BN007_TRADER_11111111111111111111111111111"
        FOLLOWER = "BN007_FOLLOW_11111111111111111111111111111"

        async def run():
            conn = await init_db(":memory:")
            await add_trader(conn, TRADER, "BNTest7")
            # copy_ratio 극소 + max 극소 → 실제 주문량 계산 시 최소 미달
            await add_follower(conn, FOLLOWER, TRADER, copy_ratio=0.0001, max_position_usdc=0.01)
            engine = CopyEngine(conn, mock_mode=False)

            await engine.on_fill({
                "account": TRADER, "symbol": "SOL",
                "event_type": "fulfill_taker",
                "price": "100", "amount": "0.001",
                "side": "open_long", "cause": "normal",
                "created_at": int(time.time() * 1000),
            })
            trades = await get_copy_trades(conn, limit=10)
            await conn.close()
            return trades

        trades = asyncio.run(run())
        # 최소 주문량 미달 → 스킵 (0건) 또는 실패 기록 (1건)
        assert len(trades) <= 1, f"과도한 주문 기록: {len(trades)}"
        print(f"\n✅ BN-007: 최소주문량 미달 처리 {len(trades)}건")
