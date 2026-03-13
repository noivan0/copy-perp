import pytest
import pytest_asyncio
import time

# asyncio_mode=auto로 async fixture 자동 지원
pytest_plugins = ('pytest_asyncio',)


# ── Rate Limit Guard ────────────────────────────────────────────────────────
# 실제 API 테스트 간 최소 간격 (CloudFront rate limit 대응)
_last_api_call = 0.0
_MIN_INTERVAL = 0.4  # 400ms


@pytest.fixture(autouse=True)
def rate_limit_guard(request):
    """실제 API 호출 테스트 간 rate limit 방어 — 400ms 최소 간격"""
    global _last_api_call
    # real_api 또는 edge case 테스트만 적용
    markers = {m.name for m in request.node.iter_markers()}
    node_name = request.node.name.lower()
    is_api_test = (
        "real" in node_name
        or "edge" in node_name
        or "cf_" in node_name
        or "pacifica" in node_name.lower()
        or "ce00" in node_name
    )
    if is_api_test:
        now = time.time()
        wait = _MIN_INTERVAL - (now - _last_api_call)
        if wait > 0:
            time.sleep(wait)
        _last_api_call = time.time()

    yield

    if is_api_test:
        _last_api_call = time.time()
