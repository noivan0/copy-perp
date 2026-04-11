"""
api/utils.py — 공통 유틸리티 (circular import 방지용 분리)

routers와 main.py 모두 여기서 import.
"""
from __future__ import annotations

import ipaddress
import os
import time
from collections import defaultdict
from threading import Lock

from fastapi import Request, HTTPException

# ── Rate Limit 공유 상태 ─────────────────────────────────
_rl_store: dict[str, list[float]] = defaultdict(list)
_rl_lock = Lock()


def _is_in_trusted_range(ip: str, cidrs: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
        for cidr in cidrs.split(","):
            cidr = cidr.strip()
            if not cidr:
                continue
            if "/" in cidr:
                if addr in ipaddress.ip_network(cidr, strict=False):
                    return True
            elif ip == cidr:
                return True
    except Exception:
        pass
    return False


def get_client_ip(request: Request) -> str:
    """실제 클라이언트 IP 추출 (X-Forwarded-For, TRUSTED_PROXY_IPS 지원)."""
    trusted_proxy = os.getenv("TRUSTED_PROXY_IPS", "")
    client_host = request.client.host if request.client else "unknown"
    if trusted_proxy and client_host != "unknown":
        if _is_in_trusted_range(client_host, trusted_proxy):
            xff = request.headers.get("X-Forwarded-For", "")
            if xff:
                return xff.split(",")[0].strip()
    return client_host


def check_rate_limit(key: str, max_calls: int, window_sec: int = 60) -> bool:
    """True = 허용, False = 한도 초과."""
    now = time.monotonic()
    with _rl_lock:
        calls = _rl_store[key]
        _rl_store[key] = [t for t in calls if now - t < window_sec]
        if len(_rl_store[key]) >= max_calls:
            return False
        _rl_store[key].append(now)
        return True


def require_rate_limit(key: str, max_calls: int, window_sec: int = 60) -> None:
    """Rate limit 초과 시 HTTPException(429) 발생."""
    if not check_rate_limit(key, max_calls, window_sec):
        raise HTTPException(
            status_code=429,
            detail={"error": "Rate limit exceeded — please wait", "code": "RATE_LIMIT_EXCEEDED"},
        )
