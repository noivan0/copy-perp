"""
api/deps.py — FastAPI 공통 의존성 주입
`from api.main import _db` 직접 참조 대신 이 모듈을 사용.

사용법:
    from api.deps import get_db, get_engine, get_monitors
    
    @router.get("/foo")
    async def foo(db = Depends(get_db)):
        ...

또는 함수 내부에서:
    from api.deps import _get_db_direct
    db = _get_db_direct()   # None 가능 — 직접 참조 fallback
"""
from __future__ import annotations
import logging
from fastapi import HTTPException

logger = logging.getLogger(__name__)


def _get_db_direct():
    """전역 _db를 안전하게 반환. None이면 503."""
    try:
        import api.main as _m
        return getattr(_m, "_db", None)
    except Exception:
        return None


def _get_engine_direct():
    try:
        import api.main as _m
        return getattr(_m, "_engine", None)
    except Exception:
        return None


def _get_monitors_direct():
    try:
        import api.main as _m
        return getattr(_m, "_monitors", {})
    except Exception:
        return {}


async def get_db():
    """FastAPI Depends용 DB 의존성. 미초기화 시 503."""
    db = _get_db_direct()
    if db is None:
        raise HTTPException(
            status_code=503,
            detail={"error": "DB not initialized", "code": "SERVICE_UNAVAILABLE"}
        )
    return db


def require_db():
    """동기 컨텍스트에서 DB 필요 시 사용. None이면 503 raise."""
    db = _get_db_direct()
    if db is None:
        raise HTTPException(
            status_code=503,
            detail={"error": "DB not initialized", "code": "SERVICE_UNAVAILABLE"}
        )
    return db
