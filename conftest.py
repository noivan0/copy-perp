import pytest
import pytest_asyncio
import time
import threading

# asyncio_mode=auto로 async fixture 자동 지원
pytest_plugins = ('pytest_asyncio',)

# ── Rate Limit Guard ────────────────────────────────────────────────────────
# 실제 API 테스트 간 최소 간격 (CloudFront / Mainnet rate limit 대응)
_last_cf_call   = 0.0  # Testnet (CloudFront)
_last_mn_call   = 0.0  # Mainnet (IP direct)
_lock = threading.Lock()

CF_MIN_INTERVAL = 0.8   # Testnet CF: 800ms
MN_MIN_INTERVAL = 0.5   # Mainnet IP: 500ms
API_MIN_INTERVAL = 0.4  # 일반 API: 400ms


def _wait_for(last_ref_name: str, interval: float):
    """글로벌 레퍼런스 기반 rate limit wait"""
    global _last_cf_call, _last_mn_call
    with _lock:
        if last_ref_name == "cf":
            now = time.time()
            wait = interval - (now - _last_cf_call)
            if wait > 0:
                time.sleep(wait)
            _last_cf_call = time.time()
        elif last_ref_name == "mn":
            now = time.time()
            wait = interval - (now - _last_mn_call)
            if wait > 0:
                time.sleep(wait)
            _last_mn_call = time.time()


@pytest.fixture(autouse=True)
def rate_limit_guard(request):
    """실제 API 호출 테스트 간 rate limit 방어"""
    node_name = request.node.name.lower()
    class_name = (request.node.cls.__name__ if request.node.cls else "").lower()
    full_name = f"{class_name}_{node_name}"

    is_testnet = any(x in full_name for x in ["tn_", "testnet", "_tnet", "cf_", "mn_b", "t1_05", "t1_03", "fc_12"])
    is_mainnet = any(x in full_name for x in ["mn_a", "mainnet", "mr_", "mn_get", "t1_04", "t1_06"])
    is_api     = any(x in full_name for x in ["real", "edge", "pacifica", "ce00"])

    if is_testnet:
        _wait_for("cf", CF_MIN_INTERVAL)
    elif is_mainnet:
        _wait_for("mn", MN_MIN_INTERVAL)

    yield

    if is_testnet:
        _wait_for("cf", CF_MIN_INTERVAL * 0.5)
    elif is_mainnet:
        _wait_for("mn", MN_MIN_INTERVAL * 0.5)
