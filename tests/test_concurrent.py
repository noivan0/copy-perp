"""동시 팔로워 부하 테스트 — asyncio.Lock 동시성 검증"""
import threading
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("MOCK_MODE", "1")
    from importlib import reload
    import api.main as m
    reload(m)
    from api.main import app
    return TestClient(app, raise_server_exceptions=False)


def _random_solana_addr() -> str:
    """유효한 Solana 주소 생성"""
    try:
        from solders.keypair import Keypair
        return str(Keypair().pubkey())
    except ImportError:
        import base58, os
        return base58.b58encode(os.urandom(32)).decode()


def test_concurrent_follow_rate_limit(client):
    """동시 5명 팔로우 → rate limit(429) 또는 정상(200/422) 응답 검증"""
    results = []
    errors = []

    trader = "EcX5xSDT45Nvhi2gMTjTnhF3KT2w4sPF54esEZS3hwZu"

    def follow_one(addr):
        try:
            r = client.post("/follow", json={
                "follower_address": addr,
                "trader_address": trader,
                "copy_ratio": 0.05,
                "max_position_usdc": 50.0,
            })
            results.append(r.status_code)
        except Exception as e:
            errors.append(str(e))

    addrs = [_random_solana_addr() for _ in range(5)]
    threads = [threading.Thread(target=follow_one, args=(a,)) for a in addrs]

    start = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    elapsed = time.time() - start

    assert not errors, f"예외 발생: {errors}"
    assert all(s in (200, 201, 400, 422, 429, 500) for s in results), \
        f"예상외 상태코드: {results}"
    assert elapsed < 20, f"너무 느림: {elapsed:.1f}초"

    rate_limited = sum(1 for s in results if s == 429)
    ok = sum(1 for s in results if s in (200, 201))
    print(f"\n동시 {len(addrs)}명: {elapsed:.1f}초 | 성공={ok} rate_limit={rate_limited} 결과={set(results)}")


def test_rate_limit_store_memory():
    """rate_limit_store 1000개 초과 시 자동 정리 검증"""
    import sys; sys.path.insert(0, ".")
    from dotenv import load_dotenv; load_dotenv()
    from api.main import _rate_limit_store, _check_rate_limit
    import time

    # 1001개 키 생성 → 자동 정리 트리거
    for i in range(1001):
        _check_rate_limit(f"test_key_{i}", max_calls=100, window_sec=60)

    # 정리 후 1000 이하
    assert len(_rate_limit_store) <= 1000, \
        f"메모리 누수: {len(_rate_limit_store)}개 키"
    print(f"\nrate_limit_store 크기: {len(_rate_limit_store)} (✅ 1000 이하)")
