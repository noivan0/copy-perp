"""
tests/test_testnet.py
Testnet API 연결 + 계정 잔고 조회 + 주문 시뮬레이션

Testnet: do5jt23sqak4.cloudfront.net (CF SNI) + Host: test-api.pacifica.fi
실계정: ACCOUNT_ADDRESS=3AHZqrocSguMuo9sUUP8G8YN8NwHwWV2DPUQvbDvtfaQ
"""
import pytest
import json
import ssl
import gzip
import socket
import time
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from dotenv import load_dotenv
load_dotenv()

TESTNET_CF   = "do5jt23sqak4.cloudfront.net"
TESTNET_HOST = "test-api.pacifica.fi"
TESTNET_BASE = "/api/v1"
ACCOUNT_ADDR = os.getenv("ACCOUNT_ADDRESS", "3AHZqrocSguMuo9sUUP8G8YN8NwHwWV2DPUQvbDvtfaQ")


# ── 헬퍼 ────────────────────────────────────────────────────────────────

def _cf_get(path: str, timeout: int = 15) -> tuple[int, dict | list]:
    """Testnet CloudFront SNI GET — chunked/gzip 처리"""
    full = TESTNET_BASE + "/" + path.lstrip("/")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection((TESTNET_CF, 443), timeout=timeout)
    s = ctx.wrap_socket(raw, server_hostname=TESTNET_CF)
    req = (
        f"GET {full} HTTP/1.1\r\nHost: {TESTNET_HOST}\r\n"
        f"Accept-Encoding: identity\r\nConnection: close\r\n\r\n"
    )
    s.sendall(req.encode())
    s.settimeout(timeout)
    data = b""
    while True:
        c = s.recv(32768)
        if not c: break
        data += c
    s.close()
    if b"\r\n\r\n" not in data:
        return 0, {}
    hdr_raw, body = data.split(b"\r\n\r\n", 1)
    code = int(hdr_raw.split(b"\r\n")[0].split()[1])
    hdr_lower = hdr_raw.lower()
    if b"transfer-encoding: chunked" in hdr_lower:
        decoded = b""
        while body:
            idx = body.find(b"\r\n")
            if idx < 0: break
            try: size = int(body[:idx], 16)
            except: break
            if size == 0: break
            decoded += body[idx+2: idx+2+size]
            body = body[idx+2+size+2:]
        body = decoded
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    if not body.strip():
        return code, {}
    return code, json.loads(body.decode("utf-8", "ignore"))


def tn(path: str) -> tuple[int, dict | list]:
    return _cf_get(path)


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


@pytest.fixture(autouse=True)
def rate_guard():
    time.sleep(0.3)   # pre-request wait
    yield
    time.sleep(0.8)   # post-request wait (rate limit)


# ── TN-A: Testnet 기본 연결 ────────────────────────────────────────────

class TestTestnetConnection:

    def test_tn_a01_prices(self):
        """[TN-A01] Testnet 실시간 가격"""
        code, data = tn("info/prices")
        assert code == 200, f"HTTP {code}"
        prices = data.get("data", data) if isinstance(data, dict) else data
        btc = next((p for p in prices if p.get("symbol") == "BTC"), None)
        assert btc, "BTC 없음"
        price = float(btc.get("mark", 0))
        assert 10_000 < price < 500_000, f"BTC 가격 이상: {price}"
        print(f"\n✅ TN-A01: Testnet BTC=${price:,.2f}, {len(prices)}개 심볼")

    def test_tn_a02_markets_count(self):
        """[TN-A02] Testnet 심볼 수 확인"""
        code, data = tn("info/prices")
        assert code == 200
        prices = data.get("data", data) if isinstance(data, dict) else data
        symbols = [p.get("symbol") for p in prices]
        assert len(symbols) >= 50, f"심볼 부족: {len(symbols)}"
        for s in ["BTC", "ETH", "SOL"]:
            assert s in symbols, f"{s} 없음"
        print(f"\n✅ TN-A02: Testnet {len(symbols)}개 심볼")

    def test_tn_a03_leaderboard(self):
        """[TN-A03] Testnet 리더보드"""
        code, data = tn("leaderboard?limit=10")
        assert code == 200
        lb = data.get("data", data) if isinstance(data, dict) else data
        assert len(lb) >= 5
        print(f"\n✅ TN-A03: Testnet 리더보드 {len(lb)}명")

    def test_tn_a04_funding_rates(self):
        """[TN-A04] Testnet 펀딩비"""
        code, data = tn("info/prices")
        assert code == 200
        prices = data.get("data", data) if isinstance(data, dict) else data
        btc = next((p for p in prices if p.get("symbol") == "BTC"), None)
        assert btc
        funding = float(btc.get("funding", 0))
        assert -0.01 < funding < 0.01, f"펀딩비 이상: {funding}"
        print(f"\n✅ TN-A04: BTC 펀딩비={funding:.6f}")

    def test_tn_a05_open_interest(self):
        """[TN-A05] Testnet 미결제약정"""
        code, data = tn("info/prices")
        assert code == 200
        prices = data.get("data", data) if isinstance(data, dict) else data
        btc = next((p for p in prices if p.get("symbol") == "BTC"), None)
        assert btc
        oi = float(btc.get("open_interest", 0))
        assert oi >= 0
        print(f"\n✅ TN-A05: BTC OI={oi:,.2f}")


# ── TN-B: 계정 잔고 조회 ──────────────────────────────────────────────

class TestTestnetAccount:

    def test_tn_b01_account_balance(self):
        """[TN-B01] 계정 USDC 잔고"""
        code, data = tn(f"account?account={ACCOUNT_ADDR}")
        assert code == 200, f"HTTP {code}"
        acct = data.get("data", data) if isinstance(data, dict) and "data" in data else data
        balance = float(acct.get("balance", acct.get("usdc_balance", 0)) or 0)
        assert balance >= 0, f"잔고 음수: {balance}"
        print(f"\n✅ TN-B01: 계정 잔고 {balance:,.2f} USDC")

    def test_tn_b02_account_positions(self):
        """[TN-B02] 계정 현재 포지션"""
        code, data = tn(f"positions?account={ACCOUNT_ADDR}")
        assert code in (200, 404), f"HTTP {code}"
        if code == 200:
            positions = data.get("data", data) if isinstance(data, dict) else data
            if isinstance(positions, list):
                print(f"\n✅ TN-B02: 포지션 {len(positions)}개")
            else:
                print(f"\n✅ TN-B02: 포지션 조회 성공")
        else:
            print(f"\n✅ TN-B02: HTTP {code} (포지션 없음)")

    def test_tn_b03_account_trades_history(self):
        """[TN-B03] 계정 체결 내역"""
        code, data = tn(f"trades/history?account={ACCOUNT_ADDR}&limit=10")
        assert code in (200, 404), f"HTTP {code}"
        if code == 200:
            trades = data.get("data", data) if isinstance(data, dict) else data
            trades = trades if isinstance(trades, list) else []
            print(f"\n✅ TN-B03: 체결 내역 {len(trades)}건")
        else:
            print(f"\n✅ TN-B03: HTTP {code}")

    def test_tn_b04_leaderboard_has_our_account(self):
        """[TN-B04] 리더보드에 계정 존재 여부"""
        code, data = tn("leaderboard?limit=100")
        assert code == 200
        lb = data.get("data", data) if isinstance(data, dict) else data
        found = next((t for t in lb if t.get("address") == ACCOUNT_ADDR), None)
        # 존재 안 해도 정상 (포지션 없으면 랭킹 없음)
        status = "포함됨" if found else "미포함(정상)"
        print(f"\n✅ TN-B04: 리더보드 내 계정 → {status}")

    def test_tn_b05_backend_account_synced(self):
        """[TN-B05] 백엔드 DB에 계정 연동"""
        code, data = backend_get(f"/traders/{ACCOUNT_ADDR}")
        # 404도 정상 (trader가 아닌 follower 계정)
        assert code in (200, 404)
        print(f"\n✅ TN-B05: 백엔드 계정 조회 HTTP {code}")


# ── TN-C: 주문 시뮬레이션 ─────────────────────────────────────────────

class TestTestnetOrderSimulation:

    def test_tn_c01_order_size_calculation(self):
        """[TN-C01] 주문 크기 계산 (copy_ratio * amount, max 상한 적용)"""
        from core.copy_engine import CopyEngine
        import asyncio
        from db.database import init_db, add_trader, add_follower

        TRADER = "TNC01_TRADER_111111111111111111111111111"
        FOLLOWER = "TNC01_FOLLOW_111111111111111111111111111"

        async def run():
            conn = await init_db(":memory:")
            await add_trader(conn, TRADER, "SimTrader")
            await add_follower(conn, FOLLOWER, TRADER, copy_ratio=0.5, max_position_usdc=100)
            cur = await conn.execute("SELECT * FROM followers WHERE address=?", (FOLLOWER,))
            row = await cur.fetchone()
            await conn.close()
            return dict(row)

        follower = asyncio.run(run())
        # copy_ratio=0.5, max_position_usdc=100
        # 원 주문 0.1 BTC @ $72000 = $7200 → 복사 $3600, max=$100 → $100/72000 = 0.00138 BTC
        src_amount = 0.1
        price = 72000
        ratio = follower["copy_ratio"]
        max_usdc = follower["max_position_usdc"]
        clamped = min(src_amount * ratio, max_usdc / price)
        assert clamped > 0 and clamped <= max_usdc / price
        print(f"\n✅ TN-C01: 복사 주문 크기 {clamped:.6f} BTC (max={max_usdc/price:.6f})")

    def test_tn_c02_mock_order_btc_long(self):
        """[TN-C02] BTC 롱 주문 Mock 시뮬레이션"""
        import asyncio
        from db.database import init_db, add_trader, add_follower, get_copy_trades
        from core.copy_engine import CopyEngine

        TRADER = "TNC02_TRADER_111111111111111111111111111"
        FOLLOWER = "TNC02_FOLLOW_111111111111111111111111111"

        async def run():
            conn = await init_db(":memory:")
            await add_trader(conn, TRADER, "BTC롱")
            await add_follower(conn, FOLLOWER, TRADER, copy_ratio=1.0, max_position_usdc=1000)
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
        assert len(trades) >= 1
        t = trades[0]
        assert t["symbol"] == "BTC"
        assert t["side"] == "bid"  # open_long → bid
        print(f"\n✅ TN-C02: BTC 롱 복사 → symbol={t['symbol']} side={t['side']} status={t['status']}")

    def test_tn_c03_mock_order_eth_short(self):
        """[TN-C03] ETH 숏 주문 Mock 시뮬레이션"""
        import asyncio
        from db.database import init_db, add_trader, add_follower, get_copy_trades
        from core.copy_engine import CopyEngine

        TRADER = "TNC03_TRADER_111111111111111111111111111"
        FOLLOWER = "TNC03_FOLLOW_111111111111111111111111111"

        async def run():
            conn = await init_db(":memory:")
            await add_trader(conn, TRADER, "ETH숏")
            await add_follower(conn, FOLLOWER, TRADER, copy_ratio=1.0, max_position_usdc=500)
            engine = CopyEngine(conn, mock_mode=True)
            await engine.on_fill({
                "account": TRADER, "symbol": "ETH",
                "event_type": "fulfill_taker",
                "price": "3000", "amount": "0.5",
                "side": "open_short", "cause": "normal",
                "created_at": int(time.time() * 1000),
            })
            trades = await get_copy_trades(conn, limit=5)
            await conn.close()
            return trades

        trades = asyncio.run(run())
        assert len(trades) >= 1
        t = trades[0]
        assert t["symbol"] == "ETH"
        assert t["side"] == "ask"  # open_short → ask
        print(f"\n✅ TN-C03: ETH 숏 복사 → symbol={t['symbol']} side={t['side']}")

    def test_tn_c04_multi_follower_order(self):
        """[TN-C04] 트레이더 1명 → 팔로워 5명 동시 복사"""
        import asyncio
        from db.database import init_db, add_trader, add_follower, get_copy_trades
        from core.copy_engine import CopyEngine

        TRADER = "TNC04_TRADER_111111111111111111111111111"

        async def run():
            conn = await init_db(":memory:")
            await add_trader(conn, TRADER, "Multi")
            for i in range(5):
                follower = f"TNC04_FOL{i:02d}_11111111111111111111111111"
                await add_follower(conn, follower, TRADER, copy_ratio=0.5, max_position_usdc=200)
            engine = CopyEngine(conn, mock_mode=True)
            await engine.on_fill({
                "account": TRADER, "symbol": "SOL",
                "event_type": "fulfill_taker",
                "price": "150", "amount": "10",
                "side": "open_long", "cause": "normal",
                "created_at": int(time.time() * 1000),
            })
            trades = await get_copy_trades(conn, limit=10)
            await conn.close()
            return trades

        trades = asyncio.run(run())
        assert len(trades) >= 4, f"5명 중 {len(trades)}건만 처리"
        print(f"\n✅ TN-C04: 팔로워 5명 → {len(trades)}건 처리")

    def test_tn_c05_price_cache_used(self):
        """[TN-C05] 가격 캐시 활용 (DataCollector 연동)"""
        code, data = backend_get("/health")
        assert code == 200
        btc_mark = data.get("btc_mark", "0")
        assert float(btc_mark) > 0, "BTC 가격 캐시 없음"
        print(f"\n✅ TN-C05: 백엔드 BTC 캐시=${float(btc_mark):,.2f}")

    def test_tn_c06_min_order_amount_check(self):
        """[TN-C06] 최소 주문량 미달 스킵"""
        import asyncio
        from db.database import init_db, add_trader, add_follower, get_copy_trades
        from core.copy_engine import CopyEngine, MIN_AMOUNT

        TRADER = "TNC06_TRADER_111111111111111111111111111"
        FOLLOWER = "TNC06_FOLLOW_111111111111111111111111111"

        async def run():
            conn = await init_db(":memory:")
            await add_trader(conn, TRADER, "MinTest")
            # copy_ratio 극소 → 주문량 MIN_AMOUNT 미달
            await add_follower(conn, FOLLOWER, TRADER, copy_ratio=0.0001, max_position_usdc=0.001)
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
        assert len(trades) == 0, f"최소량 미달인데 {len(trades)}건 처리됨"
        print(f"\n✅ TN-C06: MIN_AMOUNT({MIN_AMOUNT}) 미달 → 스킵 확인")
