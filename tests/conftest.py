
import time
import pytest

@pytest.fixture(autouse=True)
def api_rate_limit_guard():
    """API 요청 간 최소 0.5초 대기 (allorigins 레이트리밋 방지)"""
    yield
    time.sleep(0.5)
