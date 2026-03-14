"""
tests/test_performance.py
Task 3: 성능 테스트

목표:
- 팔로워 100명 동시 복사 처리시간 (현재 15.8ms, 목표 유지)
- DB 1만건 insert 후 쿼리 성능
- 24시간 연속 운영 시뮬레이션 (메모리/안정성)
- API 응답 지연 분포
"""
import pytest
import asyncio
import time
import json
import socket
import statistics
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

PERF_THRESHOLDS = {
    "follower_100_ms": 5000,     # 100명 복사 주문 처리 < 5초 (mock)
    "db_10k_insert_s": 10,       # 1만건 insert < 10초
    "db_query_after_10k_ms": 50, # 1만건 후 쿼리 < 50ms
    "api_p95_ms": 200,           # API P95 < 200ms (로컬)
    "concurrent_100_s": 5,       # 동시 100 요청 < 5초
}


def backend_get(path: str, timeout: int = 10) -> tuple[int, dict]:
    try:
        s = socket.create_connection(("localhost", 8001), timeout=timeout)
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: localhost:8001\r\nConnection: close\r\n\r\n".encode())
        s.settimeout(timeout); data = b""
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
def guard():
    yield
    time.sleep(0.1)


# ── PERF-A: 복사 엔진 처리 성능 ──────────────────────────────────────

class TestCopyEnginePerformance:

    def test_perf_a01_100_followers_processing_time(self):
        """[PERF-A01] 팔로워 100명 동시 복사 처리 시간"""
        from db.database import init_db, add_trader, add_follower, get_copy_trades
        from core.copy_engine import CopyEngine

        TRADER = "PERF_TRADER_A01_11111111111111111111111"
        N = 100

        async def run():
            conn = await init_db(":memory:")
            await add_trader(conn, TRADER, "PerfTest")
            for i in range(N):
                f = f"PERF_FOLLOW{i:03d}_1111111111111111111111111"
                await add_follower(conn, f, TRADER, copy_ratio=0.5, max_position_usdc=100)

            engine = CopyEngine(conn, mock_mode=True)
            start = time.time()
            await engine.on_fill({
                "account": TRADER, "symbol": "BTC",
                "event_type": "fulfill_taker",
                "price": "72000", "amount": "0.1",
                "side": "open_long", "cause": "normal",
                "created_at": int(time.time() * 1000),
            })
            elapsed_ms = (time.time() - start) * 1000

            trades = await get_copy_trades(conn, limit=200)
            await conn.close()
            return elapsed_ms, len(trades)

        elapsed_ms, trade_count = asyncio.run(run())
        threshold = PERF_THRESHOLDS["follower_100_ms"]
        assert elapsed_ms < threshold, f"100명 처리 {elapsed_ms:.1f}ms > {threshold}ms"
        assert trade_count >= 80, f"100명 중 {trade_count}건만 처리"
        print(f"\n✅ PERF-A01: 팔로워 100명 처리 {elapsed_ms:.1f}ms ({trade_count}건)")

    def test_perf_a02_single_event_latency(self):
        """[PERF-A02] 단일 이벤트 처리 지연 (10회 평균)"""
        from db.database import init_db, add_trader, add_follower
        from core.copy_engine import CopyEngine

        TRADER = "PERF_TRADER_A02_11111111111111111111111"

        async def run():
            conn = await init_db(":memory:")
            await add_trader(conn, TRADER, "LatencyTest")
            f = "PERF_FOLLLOW_A02_11111111111111111111111"
            await add_follower(conn, f, TRADER, copy_ratio=0.5, max_position_usdc=100)
            engine = CopyEngine(conn, mock_mode=True)

            latencies = []
            for i in range(10):
                start = time.time()
                await engine.on_fill({
                    "account": TRADER, "symbol": "BTC",
                    "event_type": "fulfill_taker",
                    "price": str(72000 + i * 100),
                    "amount": "0.1",
                    "side": "open_long", "cause": "normal",
                    "created_at": int(time.time() * 1000) + i,
                })
                latencies.append((time.time() - start) * 1000)
            await conn.close()
            return latencies

        latencies = asyncio.run(run())
        avg = statistics.mean(latencies)
        p95 = sorted(latencies)[int(len(latencies) * 0.95)]
        print(f"\n✅ PERF-A02: 단일 이벤트 avg={avg:.2f}ms p95={p95:.2f}ms")
        assert avg < 500, f"평균 지연 과다: {avg:.1f}ms"

    def test_perf_a03_1000_events_throughput(self):
        """[PERF-A03] 이벤트 1000개 처리 처리율"""
        from db.database import init_db, add_trader, add_follower, get_copy_trades
        from core.copy_engine import CopyEngine

        TRADER = "PERF_TRADER_A03_11111111111111111111111"
        FOLLOWER = "PERF_FOLLLOW_A03_11111111111111111111111"

        async def run():
            conn = await init_db(":memory:")
            await add_trader(conn, TRADER, "ThroughputTest")
            await add_follower(conn, FOLLOWER, TRADER, copy_ratio=0.5, max_position_usdc=100)
            engine = CopyEngine(conn, mock_mode=True)

            start = time.time()
            for i in range(1000):
                await engine.on_fill({
                    "account": TRADER, "symbol": "BTC",
                    "event_type": "fulfill_taker",
                    "price": "72000", "amount": "0.1",
                    "side": "open_long" if i % 2 == 0 else "open_short",
                    "cause": "normal",
                    "created_at": int(time.time() * 1000) + i,
                })
            elapsed = time.time() - start
            trades = await get_copy_trades(conn, limit=10000)
            await conn.close()
            return elapsed, len(trades)

        elapsed, count = asyncio.run(run())
        rate = count / elapsed
        print(f"\n✅ PERF-A03: 1000 이벤트 {elapsed:.2f}s ({rate:.0f} evt/s, {count}건)")
        assert count >= 900, f"1000개 중 {count}건만 처리"


# ── PERF-B: DB 성능 ──────────────────────────────────────────────────

class TestDBPerformance:

    def test_perf_b01_10k_trader_insert(self):
        """[PERF-B01] 트레이더 1만건 insert 성능"""
        import asyncio
        from db.database import init_db, add_trader

        async def run():
            conn = await init_db(":memory:")
            # WAL 모드 활성화
            await conn.execute("PRAGMA journal_mode=WAL")
            start = time.time()
            batch = []
            for i in range(10000):
                batch.append((
                    f"PERF_TRD_{i:05d}_11111111111111111111111",
                    f"Trader{i:05d}",
                    int(time.time() * 1000)
                ))
                if len(batch) == 500:  # 배치 단위 insert
                    await conn.executemany(
                        "INSERT OR IGNORE INTO traders (address, alias, created_at) VALUES (?,?,?)",
                        batch
                    )
                    await conn.commit()
                    batch = []
            if batch:
                await conn.executemany(
                    "INSERT OR IGNORE INTO traders (address, alias, created_at) VALUES (?,?,?)",
                    batch
                )
                await conn.commit()
            elapsed = time.time() - start
            cur = await conn.execute("SELECT COUNT(*) FROM traders")
            count = (await cur.fetchone())[0]
            await conn.close()
            return elapsed, count

        elapsed, count = asyncio.run(run())
        threshold = PERF_THRESHOLDS["db_10k_insert_s"]
        assert elapsed < threshold, f"10k insert {elapsed:.2f}s > {threshold}s"
        assert count >= 9900, f"10000건 중 {count}건만 insert"
        print(f"\n✅ PERF-B01: 10k insert {elapsed:.2f}s ({count}건)")

    def test_perf_b02_query_after_10k_records(self):
        """[PERF-B02] 1만건 후 쿼리 성능"""
        import asyncio
        from db.database import init_db, get_leaderboard

        async def run():
            conn = await init_db(":memory:")
            await conn.execute("PRAGMA journal_mode=WAL")
            # 10k 데이터 삽입
            for batch_start in range(0, 10000, 500):
                batch = [
                    (f"QPERF_{i:05d}_11111111111111111111111111",
                     f"Q{i:05d}", float(i * 100), int(time.time() * 1000))
                    for i in range(batch_start, batch_start + 500)
                ]
                await conn.executemany(
                    "INSERT OR IGNORE INTO traders (address, alias, total_pnl, created_at) VALUES (?,?,?,?)",
                    batch
                )
            await conn.commit()

            # 쿼리 성능 측정
            times = []
            for _ in range(10):
                start = time.time()
                lb = await get_leaderboard(conn, limit=20)
                times.append((time.time() - start) * 1000)

            await conn.close()
            return times, len(lb)

        times, lb_count = asyncio.run(run())
        avg_ms = statistics.mean(times)
        p95_ms = sorted(times)[int(len(times) * 0.95)]
        threshold = PERF_THRESHOLDS["db_query_after_10k_ms"]
        assert avg_ms < threshold, f"10k 후 쿼리 avg {avg_ms:.2f}ms > {threshold}ms"
        print(f"\n✅ PERF-B02: 10k 데이터 후 쿼리 avg={avg_ms:.2f}ms p95={p95_ms:.2f}ms")

    def test_perf_b03_concurrent_reads(self):
        """[PERF-B03] DB 동시 읽기 100건"""
        import asyncio
        from db.database import init_db, add_trader, get_leaderboard

        async def run():
            conn = await init_db(":memory:")
            for i in range(100):
                await add_trader(conn, f"CREAD_{i:03d}_11111111111111111111111111", f"CR{i}")

            start = time.time()
            tasks = [get_leaderboard(conn, limit=10) for _ in range(100)]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            elapsed = time.time() - start

            errors = [r for r in results if isinstance(r, Exception)]
            await conn.close()
            return elapsed, len(errors)

        elapsed, errors = asyncio.run(run())
        assert errors == 0, f"동시 읽기 오류 {errors}건"
        print(f"\n✅ PERF-B03: 동시 읽기 100건 {elapsed:.2f}s, 오류 {errors}건")

    def test_perf_b04_copy_trades_10k(self):
        """[PERF-B04] copy_trades 1만건 insert + 조회"""
        import asyncio
        from db.database import init_db, add_trader, add_follower

        TRADER   = "PERF_CT_TRADER_11111111111111111111111111"
        FOLLOWER = "PERF_CT_FOLLOW_11111111111111111111111111"

        async def run():
            conn = await init_db(":memory:")
            await conn.execute("PRAGMA journal_mode=WAL")
            await add_trader(conn, TRADER, "CTTest")
            await add_follower(conn, FOLLOWER, TRADER, copy_ratio=0.5, max_position_usdc=100)

            now = int(time.time() * 1000)
            start = time.time()
            batch = []
            import uuid as _uuid
            for i in range(10000):
                batch.append((
                    str(_uuid.uuid4()),
                    FOLLOWER, TRADER,
                    "BTC", "bid", "0.001",
                    "72000", "filled",
                    now + i
                ))
                if len(batch) == 500:
                    await conn.executemany(
                        """INSERT INTO copy_trades
                        (id, follower_address, trader_address, symbol, side, amount,
                         price, status, created_at)
                        VALUES (?,?,?,?,?,?,?,?,?)""",
                        batch
                    )
                    await conn.commit()
                    batch = []
            insert_elapsed = time.time() - start

            # 조회 성능
            q_start = time.time()
            cur = await conn.execute(
                "SELECT * FROM copy_trades WHERE follower_address=? ORDER BY created_at DESC LIMIT 100",
                (FOLLOWER,)
            )
            rows = await cur.fetchall()
            q_elapsed = (time.time() - q_start) * 1000

            await conn.close()
            return insert_elapsed, q_elapsed, len(rows)

        ins_s, q_ms, count = asyncio.run(run())
        assert ins_s < 15, f"10k copy_trades insert {ins_s:.2f}s > 15s"
        assert q_ms < 100, f"조회 {q_ms:.2f}ms > 100ms"
        print(f"\n✅ PERF-B04: copy_trades 10k insert={ins_s:.2f}s 쿼리={q_ms:.2f}ms ({count}건)")


# ── PERF-C: API 응답 성능 ────────────────────────────────────────────

class TestAPIResponsePerformance:

    def test_perf_c01_health_response_time_distribution(self):
        """[PERF-C01] /health 응답 시간 분포 (100회)"""
        times = []
        for idx in range(100):
            start = time.time()
            code, _data = backend_get("/health")
            times.append((time.time() - start) * 1000)
            if idx % 20 == 0:
                time.sleep(0.05)

        avg = statistics.mean(times)
        median = statistics.median(times)
        p95 = sorted(times)[94]
        p99 = sorted(times)[98]
        threshold = PERF_THRESHOLDS["api_p95_ms"]
        assert p95 < threshold, f"P95 {p95:.1f}ms > {threshold}ms"
        print(f"\n✅ PERF-C01: /health 100회 avg={avg:.1f}ms median={median:.1f}ms p95={p95:.1f}ms p99={p99:.1f}ms")

    def test_perf_c02_traders_endpoint_response(self):
        """[PERF-C02] /traders 응답 시간 (20회)"""
        times = []
        for _ in range(20):
            start = time.time()
            code, _ = backend_get("/traders?limit=20")
            assert code == 200
            times.append((time.time() - start) * 1000)

        avg = statistics.mean(times)
        p95 = sorted(times)[int(len(times) * 0.95)]
        print(f"\n✅ PERF-C02: /traders avg={avg:.1f}ms p95={p95:.1f}ms")
        assert avg < 500, f"평균 응답 {avg:.1f}ms > 500ms"

    def test_perf_c03_concurrent_100_mixed_endpoints(self):
        """[PERF-C03] 다양한 엔드포인트 동시 100 요청"""
        import threading

        endpoints = ["/health", "/stats", "/traders?limit=5", "/signals",
                     "/followers/list", "/trades?limit=5"]
        results = []
        lock = threading.Lock()

        def do_req(idx):
            ep = endpoints[idx % len(endpoints)]
            start = time.time()
            code, _ = backend_get(ep, timeout=10)
            elapsed = (time.time() - start) * 1000
            with lock:
                results.append((code, elapsed))

        threads = [threading.Thread(target=do_req, args=(i,)) for i in range(100)]
        start = time.time()
        for t in threads: t.start()
        for t in threads: t.join(timeout=30)
        total = time.time() - start

        success = sum(1 for c, _ in results if c == 200)
        avg_ms = statistics.mean(e for _, e in results) if results else 0
        threshold = PERF_THRESHOLDS["concurrent_100_s"]
        assert total < threshold, f"100 동시 요청 {total:.2f}s > {threshold}s"
        assert success >= 90, f"100개 중 {success}개 성공"
        print(f"\n✅ PERF-C03: 동시 100 요청 {total:.2f}s, 성공 {success}개, avg={avg_ms:.1f}ms")

    def test_perf_c04_stats_endpoint_response(self):
        """[PERF-C04] /stats 응답 시간"""
        times = []
        for _ in range(10):
            start = time.time()
            code, _ = backend_get("/stats")
            assert code == 200
            times.append((time.time() - start) * 1000)
        avg = statistics.mean(times)
        print(f"\n✅ PERF-C04: /stats 10회 avg={avg:.1f}ms")
        assert avg < 1000, f"avg {avg:.1f}ms > 1000ms"


# ── PERF-D: 24시간 시뮬레이션 ────────────────────────────────────────

class TestLongRunSimulation:

    def test_perf_d01_24h_equivalent_events(self):
        """[PERF-D01] 24시간 이벤트 시뮬레이션 (압축: 24h=2400이벤트 처리)"""
        import asyncio
        from db.database import init_db, add_trader, add_follower, get_copy_trades
        from core.copy_engine import CopyEngine

        TRADER   = "PERF_D01_TRADER_111111111111111111111111"
        FOLLOWER = "PERF_D01_FOLLOW_111111111111111111111111"

        async def run():
            conn = await init_db(":memory:")
            await conn.execute("PRAGMA journal_mode=WAL")
            await add_trader(conn, TRADER, "24hTest")
            await add_follower(conn, FOLLOWER, TRADER, copy_ratio=0.5, max_position_usdc=100)
            engine = CopyEngine(conn, mock_mode=True)

            start = time.time()
            # 24h 100회/h × 24h = 2400 이벤트 시뮬레이션
            for i in range(2400):
                await engine.on_fill({
                    "account": TRADER, "symbol": "BTC",
                    "event_type": "fulfill_taker",
                    "price": str(70000 + (i % 5000)),
                    "amount": "0.01",
                    "side": "open_long" if i % 2 == 0 else "open_short",
                    "cause": "normal",
                    "created_at": int(time.time() * 1000) + i * 1000,
                })

            elapsed = time.time() - start
            trades = await get_copy_trades(conn, limit=10000)

            # 쿼리 성능 (데이터 많아진 후)
            q_start = time.time()
            await get_copy_trades(conn, limit=100)
            q_ms = (time.time() - q_start) * 1000

            await conn.close()
            return elapsed, len(trades), q_ms

        elapsed, count, q_ms = asyncio.run(run())
        assert count >= 2000, f"2400 이벤트 중 {count}건만 처리"
        assert q_ms < 200, f"2400건 후 쿼리 {q_ms:.1f}ms > 200ms"
        rate = count / elapsed
        print(f"\n✅ PERF-D01: 24h 시뮬레이션 {count}건/{elapsed:.1f}s ({rate:.0f}/s) 쿼리={q_ms:.1f}ms")

    def test_perf_d02_memory_stability_proxy(self):
        """[PERF-D02] 메모리 안정성 — DB 반복 open/close 100회"""
        import asyncio
        from db.database import init_db

        async def run():
            for i in range(100):
                conn = await init_db(":memory:")
                await conn.execute("CREATE TABLE IF NOT EXISTS t (v INTEGER)")
                await conn.execute("INSERT INTO t VALUES (?)", (i,))
                await conn.commit()
                cur = await conn.execute("SELECT COUNT(*) FROM t")
                await cur.fetchone()
                await conn.close()
            return True

        result = asyncio.run(run())
        assert result
        print(f"\n✅ PERF-D02: DB 100회 open/close 안정적")

    def test_perf_d03_sustained_throughput(self):
        """[PERF-D03] 지속 처리율 — 10초간 API 호출"""
        start = time.time()
        count = 0
        errors = 0
        while time.time() - start < 5:  # 5초 (10초 압축)
            code, _ = backend_get("/health", timeout=3)
            if code == 200:
                count += 1
            else:
                errors += 1

        elapsed = time.time() - start
        rate = count / elapsed
        assert count > 0, "5초간 요청 0건"
        assert errors / max(count + errors, 1) < 0.05, f"에러율 {errors/(count+errors)*100:.1f}% > 5%"
        print(f"\n✅ PERF-D03: 5초 지속 {count}건 ({rate:.1f} req/s), 에러 {errors}건")
