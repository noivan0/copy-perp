"""
test_concurrent.py — 동시 팔로워 부하 테스트
목표: 5명 동시 팔로우 요청을 15초 이내에 처리하고
      모든 응답이 200 / 422 / 429 중 하나임을 확인.
"""
import time
import threading
import pytest

from fastapi.testclient import TestClient
from api.main import app


# ── 헬퍼 ─────────────────────────────────────────────────────────────────────

def _generate_solana_addresses(n: int) -> list[str]:
    """테스트용 유효한 Solana 주소 n개 생성"""
    try:
        from solders.keypair import Keypair
        return [str(Keypair().pubkey()) for _ in range(n)]
    except ImportError:
        # solders 없으면 미리 준비한 주소 사용 (base58, 44자, 실제 유효 주소)
        fallback = [
            "EcX5xSDT45Nvhi2gMTjTnhF3KT2w4sPF54esEZS3hwZu",
            "4UBH19qUbXEaqyz9fKrFHuvj8BPMoM87H71s1YPKyGYq",
            "7C3sXQ6KvXJLkYGwzjNy2BHpkfEnRHzzfVAgUS64CDEd",
            "7gV81bz99MUBVb2aLYxW7MG1RMDdRdJYTPyC2syjba8y",
            "3rXoG6i55P7D1Q3tYsB7Unds8nBtKh7vH5VUyMDpWkSe",
        ]
        return fallback[:n]


# ── 테스트 ────────────────────────────────────────────────────────────────────

def test_concurrent_follow():
    """동시 팔로워 5명 팔로우 요청 — 15초 이내, 응답 코드 정상"""
    TRADER = "EcX5xSDT45Nvhi2gMTjTnhF3KT2w4sPF54esEZS3hwZu"
    CONCURRENT = 5

    addrs = _generate_solana_addresses(CONCURRENT)
    results: list[int] = []
    errors: list[str] = []

    def follow_one(addr: str) -> None:
        try:
            with TestClient(app) as c:
                r = c.post(
                    "/follow",
                    json={
                        "follower_address": addr,
                        "trader_address": TRADER,
                        "copy_ratio": 0.05,
                        "max_position_usdc": 50.0,
                    },
                    timeout=20,
                )
                results.append(r.status_code)
        except Exception as e:
            errors.append(str(e))
            results.append(-1)

    threads = [threading.Thread(target=follow_one, args=(a,)) for a in addrs]
    start = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    elapsed = time.time() - start

    print(f"\n동시 {CONCURRENT}명 처리: {elapsed:.1f}초, 결과: {set(results)}")
    if errors:
        print(f"에러: {errors}")

    # 검증
    assert len(results) == CONCURRENT, f"응답 수 불일치: {len(results)} != {CONCURRENT}"
    assert all(
        s in (200, 422, 429) for s in results
    ), f"예상 외 상태코드 발생: {results}"
    assert elapsed < 15, f"처리 시간 초과: {elapsed:.1f}초 (limit: 15초)"


def test_concurrent_markets_read():
    """동시 시장 데이터 읽기 10건 — 빠른 읽기 엔드포인트 안정성 확인"""
    CONCURRENT = 10
    results: list[int] = []

    def read_markets() -> None:
        with TestClient(app) as c:
            r = c.get("/markets", timeout=10)
            results.append(r.status_code)

    threads = [threading.Thread(target=read_markets) for _ in range(CONCURRENT)]
    start = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    elapsed = time.time() - start

    print(f"\n동시 {CONCURRENT}건 /markets: {elapsed:.1f}초, 결과: {set(results)}")
    assert len(results) == CONCURRENT
    assert all(s in (200, 429) for s in results), f"예상 외 상태코드: {results}"
    assert elapsed < 10, f"처리 시간 초과: {elapsed:.1f}초"


def test_health_under_load():
    """헬스체크 동시 5건 — 항상 200 반환해야 함 (Rate limit 300/min)"""
    CONCURRENT = 5
    results: list[int] = []

    def check_health() -> None:
        with TestClient(app) as c:
            r = c.get("/healthz", timeout=5)
            results.append(r.status_code)

    threads = [threading.Thread(target=check_health) for _ in range(CONCURRENT)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert all(s == 200 for s in results), f"/healthz 실패: {results}"
