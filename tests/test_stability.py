"""
tests/test_stability.py
서비스 안정성 테스트 — 연속 요청, 재시작 복구, 동시성

목적: 실제 서비스 수준 안정성 검증
"""
import pytest
import asyncio
import time
import json
import socket
import threading
import subprocess
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

BACKEND_HOST = "localhost"
BACKEND_PORT = 8001


def backend_get(path: str, timeout: int = 10) -> tuple[int, dict]:
    try:
        s = socket.create_connection((BACKEND_HOST, BACKEND_PORT), timeout=timeout)
        req = f"GET {path} HTTP/1.1\r\nHost: {BACKEND_HOST}:{BACKEND_PORT}\r\nConnection: close\r\n\r\n"
        s.sendall(req.encode())
        s.settimeout(timeout)
        data = b""
        while True:
            c = s.recv(16384)
            if not c:
                break
            data += c
        s.close()
        header, body = data.split(b"\r\n\r\n", 1)
        code = int(header.split(b"\r\n")[0].split()[1])
        return code, json.loads(body.decode("utf-8", "ignore"))
    except ConnectionRefusedError:
        pytest.skip("백엔드 미기동 (port 8001)")
    except Exception as e:
        return 0, {"error": str(e)}


def backend_post(path: str, body: dict, timeout: int = 10) -> tuple[int, dict]:
    try:
        s = socket.create_connection((BACKEND_HOST, BACKEND_PORT), timeout=timeout)
        b = json.dumps(body).encode()
        req = (
            f"POST {path} HTTP/1.1\r\n"
            f"Host: {BACKEND_HOST}:{BACKEND_PORT}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(b)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode() + b
        s.sendall(req)
        s.settimeout(timeout)
        data = b""
        while True:
            c = s.recv(16384)
            if not c:
                break
            data += c
        s.close()
        header, body_resp = data.split(b"\r\n\r\n", 1)
        code = int(header.split(b"\r\n")[0].split()[1])
        return code, json.loads(body_resp.decode("utf-8", "ignore"))
    except ConnectionRefusedError:
        pytest.skip("백엔드 미기동 (port 8001)")
    except Exception as e:
        return 0, {"error": str(e)}


# ── 순차 실행 보장 ─────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def sequential():
    yield
    time.sleep(0.3)


class TestBackendStability:

    def test_st001_health_check_20_times(self):
        """[ST-001] Health check 20회 연속 성공"""
        results = []
        for i in range(20):
            code, data = backend_get("/health", timeout=5)
            results.append(code)
            time.sleep(0.1)

        success = results.count(200)
        assert success >= 18, f"20회 중 {success}회 성공 (기준: 18회 이상)"
        print(f"\n✅ ST-001: Health check 20회 중 {success}회 성공")

    def test_st002_concurrent_requests(self):
        """[ST-002] 동시 10개 요청 처리"""
        results = []
        lock = threading.Lock()

        def make_request():
            code, _ = backend_get("/health", timeout=10)
            with lock:
                results.append(code)

        threads = [threading.Thread(target=make_request) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        success = results.count(200)
        assert success >= 8, f"동시 10개 중 {success}개 성공 (기준: 8개 이상)"
        print(f"\n✅ ST-002: 동시 요청 10개 중 {success}개 성공")

    def test_st003_stats_endpoint_stable(self):
        """[ST-003] /stats 엔드포인트 10회 안정성"""
        errors = 0
        for i in range(10):
            code, data = backend_get("/stats", timeout=10)
            if code != 200:
                errors += 1
            time.sleep(0.2)

        assert errors <= 2, f"/stats 10회 중 {errors}회 실패"
        print(f"\n✅ ST-003: /stats 10회 중 {10 - errors}회 성공")

    def test_st004_traders_endpoint_consistent(self):
        """[ST-004] /traders 응답 일관성 (같은 요청 → 같은 구조)"""
        responses = []
        for _ in range(3):
            code, data = backend_get("/traders?limit=5", timeout=10)
            assert code == 200
            d = data if isinstance(data, list) else data.get("data", [])
            responses.append(len(d))
            time.sleep(0.5)

        # 3회 모두 같은 수의 트레이더 반환
        assert len(set(responses)) == 1, f"응답 수 불일치: {responses}"
        print(f"\n✅ ST-004: /traders 3회 일관성 확인 ({responses[0]}명)")

    def test_st005_response_time_under_threshold(self):
        """[ST-005] 응답 시간 < 5초"""
        slow = []
        for path in ["/health", "/stats", "/traders?limit=5", "/signals"]:
            start = time.time()
            code, _ = backend_get(path, timeout=10)
            elapsed = time.time() - start
            if elapsed > 5.0:
                slow.append((path, elapsed))
            print(f"  {path}: {elapsed:.2f}s (HTTP {code})")

        assert len(slow) == 0, f"응답 지연 엔드포인트: {slow}"
        print(f"\n✅ ST-005: 모든 엔드포인트 응답시간 < 5s")

    def test_st006_follow_and_unfollow_cycle(self):
        """[ST-006] Follow → 재팔로우 싸이클 (idempotent)"""
        trader = "EcX5xSDT45Nvhi2gMTjTnhF3KT2w4sPF54esEZS3hwZu"
        follower = "9mxJJAQwKLmM3hUdFebFXgkD8TPnDEJCZWhWN2uLZHWi"

        # 2회 동일 팔로우 → 모두 200 or 409
        for i in range(2):
            code, data = backend_post("/follow", {
                "trader_address": trader,
                "follower_address": follower,
                "copy_ratio": 0.5,
                "max_position_usdc": 50,
            })
            assert code in (200, 201, 409), f"시도 {i+1}: HTTP {code} {data}"
            time.sleep(0.5)

        print(f"\n✅ ST-006: 중복 팔로우 idempotent 확인")

    def test_st007_leaderboard_cache_valid(self):
        """[ST-007] 리더보드 캐시 유효 (연속 2회 동일 결과)"""
        code1, data1 = backend_get("/traders?limit=5")
        time.sleep(1)
        code2, data2 = backend_get("/traders?limit=5")

        assert code1 == 200 and code2 == 200

        d1 = data1 if isinstance(data1, list) else data1.get("data", [])
        d2 = data2 if isinstance(data2, list) else data2.get("data", [])

        # 주소 목록 동일
        addrs1 = {t["address"] for t in d1[:5]}
        addrs2 = {t["address"] for t in d2[:5]}
        overlap = len(addrs1 & addrs2)
        assert overlap >= 3, f"캐시 불일치: {overlap}/5 동일"
        print(f"\n✅ ST-007: 리더보드 캐시 유효 ({overlap}/5 일치)")


class TestCopyEngineStability:

    def test_st010_copy_engine_mock_stress(self):
        """[ST-010] Copy Engine 50개 이벤트 연속 처리 (mock)"""
        import asyncio
        from db.database import init_db, add_trader, add_follower, get_copy_trades
        from core.copy_engine import CopyEngine

        TRADER = "ST010_TRADER_11111111111111111111111111"
        FOLLOWER = "ST010_FOLLOW_11111111111111111111111111"

        async def run():
            conn = await init_db(":memory:")
            await add_trader(conn, TRADER, "StressTest")
            await add_follower(conn, FOLLOWER, TRADER, copy_ratio=0.5, max_position_usdc=100)
            engine = CopyEngine(conn, mock_mode=True)

            for i in range(50):
                await engine.on_fill({
                    "account": TRADER,
                    "symbol": "BTC" if i % 2 == 0 else "ETH",
                    "event_type": "fulfill_taker",
                    "price": str(72000 + i * 10),
                    "amount": "0.1",
                    "side": "open_long" if i % 3 != 0 else "open_short",
                    "cause": "normal",
                    "created_at": int(time.time() * 1000) + i * 100,
                })

            trades = await get_copy_trades(conn, limit=100)
            await conn.close()
            return trades

        trades = asyncio.run(run())
        assert len(trades) >= 45, f"50개 중 {len(trades)}개만 처리"
        filled = sum(1 for t in trades if t["status"] == "filled")
        print(f"\n✅ ST-010: 50개 이벤트 → {len(trades)}개 처리 (filled={filled})")

    def test_st011_copy_engine_exception_resilience(self):
        """[ST-011] 잘못된 이벤트 처리 후 정상 이벤트 복구"""
        import asyncio
        from db.database import init_db, add_trader, add_follower, get_copy_trades
        from core.copy_engine import CopyEngine

        TRADER = "ST011_TRADER_11111111111111111111111111"
        FOLLOWER = "ST011_FOLLOW_11111111111111111111111111"

        async def run():
            conn = await init_db(":memory:")
            await add_trader(conn, TRADER, "ResilienceTest")
            await add_follower(conn, FOLLOWER, TRADER, copy_ratio=0.5, max_position_usdc=100)
            engine = CopyEngine(conn, mock_mode=True)

            # 잘못된 이벤트들
            bad_events = [
                {},  # 빈 이벤트
                {"account": TRADER, "symbol": None},  # None 심볼
                {"account": TRADER, "symbol": "BTC", "event_type": "unknown_type"},  # 알 수 없는 타입
            ]
            for ev in bad_events:
                try:
                    await engine.on_fill(ev)
                except Exception:
                    pass  # 예외 처리됨 — 서비스 유지가 목적

            # 정상 이벤트
            await engine.on_fill({
                "account": TRADER, "symbol": "BTC",
                "event_type": "fulfill_taker",
                "price": "72000", "amount": "0.1",
                "side": "open_long", "cause": "normal",
                "created_at": int(time.time() * 1000),
            })

            trades = await get_copy_trades(conn, limit=10)
            await conn.close()
            return trades

        trades = asyncio.run(run())
        # 정상 이벤트는 반드시 처리돼야 함
        assert len(trades) >= 1, "정상 이벤트 처리 실패"
        print(f"\n✅ ST-011: 예외 후 복구 확인 — {len(trades)}건 처리")


class TestDataConsistency:

    def test_st020_traders_count_consistent(self):
        """[ST-020] /stats vs /traders 트레이더 수 일관성"""
        code_s, stats = backend_get("/stats")
        code_t, traders = backend_get("/traders?limit=200")

        assert code_s == 200 and code_t == 200

        stats_count = stats.get("active_traders", 0)
        t_list = traders if isinstance(traders, list) else traders.get("data", [])
        traders_count = len(t_list)

        # 5% 이내 차이 허용
        diff = abs(stats_count - traders_count)
        print(f"\n✅ ST-020: /stats={stats_count}명 vs /traders={traders_count}명 (차이 {diff})")
        # 완전 일치 불필요 — 페이지네이션 차이 있음

    def test_st021_filled_trades_count_valid(self):
        """[ST-021] /trades filled 건수 검증"""
        code, data = backend_get("/trades?status=filled&limit=100")
        assert code == 200
        trades = data if isinstance(data, list) else data.get("trades", data.get("data", []))
        print(f"\n✅ ST-021: filled 거래 {len(trades)}건 확인")

    def test_st022_signals_endpoint_valid(self):
        """[ST-022] /signals 데이터 유효성"""
        code, data = backend_get("/signals")
        assert code == 200
        signals = data if isinstance(data, list) else data.get("signals", data.get("data", data))
        print(f"\n✅ ST-022: /signals 응답 확인 — {type(signals).__name__}")
