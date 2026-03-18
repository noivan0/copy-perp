"""
트레이더 라우터
GET  /traders                      — 리더보드
POST /traders                      — 트레이더 등록
GET  /traders/{address}            — 트레이더 상세 + 통계
GET  /traders/{address}/trades     — 체결 이력
GET  /traders/{address}/followers  — 팔로워 목록
"""
from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel
from typing import Optional

from db.database import add_trader, get_leaderboard, get_followers
from core.stats import compute_trader_stats, get_trader_stats
from core.mock import MOCK_TRADERS, mock_fill_event
from pacifica.client import PacificaClient

_pacifica = PacificaClient()

router = APIRouter(prefix="/traders", tags=["traders"])


class TraderRegister(BaseModel):
    address: str
    alias: Optional[str] = ""


@router.get("")
async def list_traders(limit: int = 20, mock: bool = False):
    """리더보드 — PnL 기준 정렬
    mock=true: Mock 데이터 강제 반환
    mock=false (기본): DB 우선, 비어있으면 Mock 폴백
    """
    if mock:
        sorted_traders = sorted(MOCK_TRADERS, key=lambda x: x["total_pnl"], reverse=True)
        return {"data": sorted_traders[:limit], "source": "mock", "count": len(sorted_traders[:limit])}

    from api.main import _db
    leaders = await get_leaderboard(_db, limit)
    if leaders:
        return {"data": [dict(r) for r in leaders], "source": "db", "count": len(leaders)}

    # DB 비어있으면 실제 Pacifica 리더보드 시도
    try:
        real_lb = _pacifica.get_leaderboard(limit=limit)
        if real_lb:
            return {"data": real_lb, "source": "pacifica_live", "count": len(real_lb)}
    except Exception:
        pass

    # 최후 폴백: Mock 데이터
    sorted_traders = sorted(MOCK_TRADERS, key=lambda x: x["total_pnl"], reverse=True)
    return {"data": sorted_traders[:limit], "source": "mock_fallback", "count": len(sorted_traders[:limit])}


@router.post("")
async def register_trader(body: TraderRegister, background_tasks: BackgroundTasks):
    from api.main import _db, _engine, _monitors
    from core.position_monitor import RestPositionMonitor  # WS 차단 환경 → REST 폴링 사용

    await add_trader(_db, body.address, body.alias)

    if body.address not in _monitors:
        monitor = RestPositionMonitor(body.address, _engine.on_fill)
        _monitors[body.address] = monitor
        background_tasks.add_task(monitor.start)

    return {"ok": True, "address": body.address, "alias": body.alias, "monitoring": True}


@router.get("/{address}")
async def get_trader(address: str):
    """트레이더 상세 + 성과 통계 — DB 우선, Mock 폴백"""
    from api.main import _db

    # DB 우선 조회
    async with _db.execute("SELECT * FROM traders WHERE address = ?", (address,)) as cur:
        row = await cur.fetchone()

    if row:
        import asyncio
        stats = await asyncio.get_event_loop().run_in_executor(
            None, get_trader_stats, address
        )
        return {"data": {**dict(row), **stats}, "source": "db"}

    # DB에 없으면 Mock에서 찾기
    mock = next((t for t in MOCK_TRADERS if t["address"] == address), None)
    if mock:
        return {"data": mock, "source": "mock"}

    raise HTTPException(404, "트레이더를 찾을 수 없습니다")


@router.get("/{address}/trades")
async def get_trader_trades(address: str, limit: int = 50):
    from api.main import _db
    async with _db.execute(
        "SELECT * FROM copy_trades WHERE trader_address = ? ORDER BY created_at DESC LIMIT ?",
        (address, limit)
    ) as cur:
        rows = await cur.fetchall()
    return {"data": [dict(r) for r in rows], "count": len(rows)}


@router.get("/{address}/followers")
async def get_trader_followers(address: str):
    from api.main import _db
    rows = await get_followers(_db, address)
    return {"data": [dict(r) for r in rows], "count": len(rows)}
