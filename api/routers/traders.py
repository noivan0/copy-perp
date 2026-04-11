"""
트레이더 라우터
GET  /traders                      — 리더보드
POST /traders                      — 트레이더 등록
GET  /traders/{address}            — 트레이더 상세 + 통계
GET  /traders/{address}/trades     — 체결 이력
GET  /traders/{address}/followers  — 팔로워 목록
"""
import logging
from fastapi import APIRouter, HTTPException, BackgroundTasks, Request, Query
from pydantic import BaseModel
from typing import Optional

from db.database import add_trader, get_leaderboard, get_followers
from core.stats import compute_trader_stats, get_trader_stats
from core.mock import MOCK_TRADERS, mock_fill_event
from pacifica.client import PacificaClient

logger = logging.getLogger(__name__)
_pacifica = PacificaClient()

_TRADERS_CACHE: dict = {}   # {key: (ts, payload)}
_TRADERS_CACHE_TTL = 120     # 초

router = APIRouter(prefix="/traders", tags=["traders"])


class TraderRegister(BaseModel):
    address: str
    alias: Optional[str] = ""


@router.get("")
async def list_traders(request: Request, limit: int = Query(50, ge=1, le=100, description="최대 100명"), offset: int = Query(0, ge=0, description="페이지 오프셋"), mock: bool = False):
    """리더보드 — PnL 기준 정렬
    mock=true: Mock 데이터 강제 반환
    mock=false (기본): DB 우선, 비어있으면 Mock 폴백
    """
    req_id = getattr(request.state, "request_id", "??")

    # Rate limit: IP당 분당 60회
    from api.utils import get_client_ip as _gcip, check_rate_limit as _crl
    client_ip = _gcip(request)
    if not _crl(f"traders:{client_ip}", 60, 60):
        raise HTTPException(
            status_code=429,
            detail={"error": "Rate limit exceeded", "code": "RATE_LIMIT_EXCEEDED"}
        )

    # limit 검증 (FastAPI Query le=100과 일치)
    if limit < 1 or limit > 100:
        raise HTTPException(
            status_code=400,
            detail={"error": "limit must be between 1 and 100", "code": "INVALID_LIMIT"}
        )

    # 인메모리 캐시 (TTL 120초)
    import time as _time_mod
    _cache_key = (limit, offset)
    _cached = _TRADERS_CACHE.get(_cache_key)
    if _cached and (_time_mod.time() - _cached[0]) < _TRADERS_CACHE_TTL:
        return {**_cached[1], "_cached": True}

    if mock:
        sorted_traders = sorted(MOCK_TRADERS, key=lambda x: x["total_pnl"], reverse=True)
        return {"data": sorted_traders[:limit], "source": "mock", "count": len(sorted_traders[:limit])}

    try:
        from api.deps import _get_db_direct
        _db = _get_db_direct()
        leaders = await get_leaderboard(_db, limit, offset) if _db else []
        if leaders:
            def _enrich(r: dict) -> dict:
                """composite_score 등 파생 필드 추가"""
                pnl = float(r.get("total_pnl", 0) or 0)
                wr  = float(r.get("win_rate", 0) or 0)
                eq  = float(r.get("equity", 0) or 0)
                roi = pnl / eq if eq > 0 else 0
                # total_trades = win_count + lose_count (DB에 total_trades 컬럼 없음)
                win  = int(r.get("win_count", 0) or 0)
                lose = int(r.get("lose_count", 0) or 0)
                total_trades = win + lose
                # composite_score: WR 40% + ROI 40% + PnL 정규화 20%
                composite = round(wr * 0.4 + min(roi, 2.0) * 0.4 + min(pnl / 100000, 1.0) * 0.2, 4)
                return {**r,
                        "composite_score": composite,
                        "roi": round(roi, 4),
                        "total_trades": total_trades}
            _result = {"data": [_enrich(dict(r)) for r in leaders], "source": "db", "count": len(leaders)}
            _TRADERS_CACHE[_cache_key] = (_time_mod.time(), _result)
            return _result
    except Exception as e:
        logger.warning(f"[{req_id}] 트레이더 DB 조회 실패: {e}")

    # DB 비어있으면 실제 Pacifica 리더보드 시도
    try:
        real_lb = _pacifica.get_leaderboard(limit=limit)
        if real_lb:
            return {"data": real_lb, "source": "pacifica_live", "count": len(real_lb)}
    except Exception as e:
        logger.warning(f"[{req_id}] Pacifica 리더보드 조회 실패: {e}")

    # 최후 폴백: Mock 데이터
    sorted_traders = sorted(MOCK_TRADERS, key=lambda x: x["total_pnl"], reverse=True)
    return {"data": sorted_traders[:limit], "source": "mock_fallback", "count": len(sorted_traders[:limit])}


@router.post("")
async def register_trader(body: TraderRegister, background_tasks: BackgroundTasks, request: Request):
    from api.deps import _get_db_direct, _get_engine_direct, _get_monitors_direct
    from api.main import _is_valid_solana_address
    _db = _get_db_direct()
    _engine = _get_engine_direct()
    _monitors = _get_monitors_direct()
    from core.position_monitor import RestPositionMonitor

    req_id = getattr(request.state, "request_id", "??")

    # 주소 검증
    if not _is_valid_solana_address(body.address):
        raise HTTPException(
            status_code=400,
            detail={"error": "Invalid Solana address (address)", "code": "INVALID_ADDRESS"}
        )

    try:
        await add_trader(_db, body.address, body.alias)
    except Exception as e:
        logger.error(f"[{req_id}] 트레이더 등록 실패: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "Failed to register trader", "code": "INTERNAL_SERVER_ERROR"}
        )

    if body.address not in _monitors:
        try:
            monitor = RestPositionMonitor(body.address, _engine.on_fill)
            _monitors[body.address] = monitor
            background_tasks.add_task(monitor.start)
        except Exception as e:
            logger.warning(f"[{req_id}] 모니터 시작 실패 (등록은 성공): {e}")

    return {"ok": True, "address": body.address, "alias": body.alias, "monitoring": True}


@router.get("/{address}")
async def get_trader(address: str, request: Request):
    """트레이더 상세 + 성과 통계 — DB 우선, Mock 폴백"""
    from api.deps import _get_db_direct
    from api.main import _is_valid_solana_address
    _db = _get_db_direct()
    import asyncio

    req_id = getattr(request.state, "request_id", "??")

    if not _is_valid_solana_address(address):
        raise HTTPException(
            status_code=400,
            detail={"error": "Invalid Solana address format", "code": "INVALID_ADDRESS"}
        )

    try:
        # DB 우선 조회
        async with _db.execute("SELECT * FROM traders WHERE address = ?", (address,)) as cur:
            row = await cur.fetchone()
    except Exception as e:
        logger.error(f"[{req_id}] 트레이더 조회 DB 오류: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "Failed to load trader info", "code": "INTERNAL_SERVER_ERROR"}
        )

    if row:
        try:
            stats = await asyncio.get_event_loop().run_in_executor(
                None, get_trader_stats, address
            )
        except Exception as e:
            logger.warning(f"[{req_id}] 트레이더 stats 조회 실패: {e}")
            stats = {}
        return {"data": {**dict(row), **stats}, "source": "db"}

    # DB에 없으면 Mock에서 찾기
    mock = next((t for t in MOCK_TRADERS if t["address"] == address), None)
    if mock:
        return {"data": mock, "source": "mock"}

    raise HTTPException(
        status_code=404,
        detail={"error": "Trader not found", "code": "NOT_FOUND"}
    )


@router.get("/{address}/trades")
async def get_trader_trades(address: str, limit: int = 50, request: Request = None):
    from api.deps import _get_db_direct
    _db = _get_db_direct()
    req_id = getattr(request.state, "request_id", "??") if request else "??"

    if limit < 1 or limit > 500:
        raise HTTPException(
            status_code=400,
            detail={"error": "limit must be between 1 and 500", "code": "INVALID_LIMIT"}
        )
    try:
        async with _db.execute(
            "SELECT * FROM copy_trades WHERE trader_address = ? ORDER BY created_at DESC LIMIT ?",
            (address, limit)
        ) as cur:
            rows = await cur.fetchall()
        return {"data": [dict(r) for r in rows], "count": len(rows)}
    except Exception as e:
        logger.error(f"[{req_id}] 트레이더 거래 내역 조회 실패: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "Failed to load trade history", "code": "INTERNAL_SERVER_ERROR"}
        )


@router.get("/{address}/followers")
async def get_trader_followers(address: str, request: Request = None):
    from api.deps import _get_db_direct
    _db = _get_db_direct()
    req_id = getattr(request.state, "request_id", "??") if request else "??"
    try:
        rows = await get_followers(_db, address) if _db else []
        return {"data": [dict(r) for r in rows], "count": len(rows)}
    except Exception as e:
        logger.error(f"[{req_id}] 팔로워 목록 조회 실패: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "Failed to load follower list", "code": "INTERNAL_SERVER_ERROR"}
        )
