"""
Copy Perp FastAPI 서버 — W1 라우터 구현

엔드포인트:
  GET  /health
  GET  /traders          — 리더보드
  POST /traders          — 트레이더 등록
  POST /followers        — 팔로워 등록 (builder code 승인 포함)
  GET  /followers/{addr} — 팔로워 현황
  GET  /copy-trades      — 복사 주문 이력
  POST /copy-trades/test — 수동 테스트 복사 (개발용)
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
import uvicorn

from db.database import init_db, add_trader, add_follower, get_followers, get_leaderboard
from core.copy_engine import CopyEngine
from core.position_monitor import PositionMonitor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 글로벌 상태
db = None
engine = None
monitors: dict[str, PositionMonitor] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db, engine
    db = await init_db()
    engine = CopyEngine(db)
    logger.info("✅ Copy Perp 서버 시작")
    yield
    if db:
        await db.close()
    logger.info("서버 종료")


app = FastAPI(
    title="Copy Perp API",
    description="Decentralized copy trading on Pacifica",
    version="0.1.0",
    lifespan=lifespan,
)


# ── 스키마 ──────────────────────────────────

class TraderRegister(BaseModel):
    address: str
    alias: Optional[str] = ""


class FollowerRegister(BaseModel):
    address: str
    trader_address: str
    copy_ratio: float = 1.0
    max_position_usdc: float = 100.0


class TestCopyEvent(BaseModel):
    trader_address: str
    symbol: str = "BTC"
    side: str = "open_long"
    amount: str = "0.01"


# ── 헬스체크 ──────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": int(time.time() * 1000)}


# ── 트레이더 ──────────────────────────────────

@app.get("/traders")
async def list_traders(limit: int = 20):
    """리더보드 — PnL 기준 정렬"""
    leaders = await get_leaderboard(db, limit)
    return {"data": [dict(r) for r in leaders]}


@app.post("/traders")
async def register_trader(body: TraderRegister):
    """트레이더 등록"""
    await add_trader(db, body.address, body.alias)
    return {"ok": True, "address": body.address}


# ── 팔로워 ──────────────────────────────────

@app.post("/followers")
async def register_follower(body: FollowerRegister, background_tasks: BackgroundTasks):
    """
    팔로워 등록
    1. DB에 저장
    2. 트레이더 포지션 모니터 시작 (미시작인 경우)
    """
    await add_follower(
        db,
        body.address,
        body.trader_address,
        body.copy_ratio,
        body.max_position_usdc,
    )

    # 모니터 시작 (한 트레이더당 1개)
    if body.trader_address not in monitors:
        monitor = PositionMonitor(body.trader_address, engine.on_fill)
        monitors[body.trader_address] = monitor
        background_tasks.add_task(monitor.start)
        logger.info(f"포지션 모니터 시작: {body.trader_address[:8]}...")

    return {
        "ok": True,
        "follower": body.address,
        "trader": body.trader_address,
        "copy_ratio": body.copy_ratio,
        "note": "Builder Code 승인은 프론트엔드에서 서명 처리 필요"
    }


@app.get("/followers/{address}")
async def get_follower_status(address: str):
    """팔로워 현황"""
    async with db.execute(
        "SELECT * FROM followers WHERE address = ?", (address,)
    ) as cur:
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "팔로워를 찾을 수 없습니다")
    return {"data": dict(row)}


# ── 복사 주문 ──────────────────────────────────

@app.get("/copy-trades")
async def list_copy_trades(limit: int = 50, follower: Optional[str] = None):
    """복사 주문 이력"""
    if follower:
        query = "SELECT * FROM copy_trades WHERE follower_address = ? ORDER BY created_at DESC LIMIT ?"
        params = (follower, limit)
    else:
        query = "SELECT * FROM copy_trades ORDER BY created_at DESC LIMIT ?"
        params = (limit,)

    async with db.execute(query, params) as cur:
        rows = await cur.fetchall()
    return {"data": [dict(r) for r in rows]}


@app.post("/copy-trades/test")
async def test_copy(body: TestCopyEvent):
    """수동 복사 이벤트 트리거 (개발/테스트용)"""
    event = {
        "account": body.trader_address,
        "symbol": body.symbol,
        "side": body.side,
        "amount": body.amount,
        "price": "0",
        "event_type": "fulfill_taker",
        "cause": "normal",
        "created_at": int(time.time() * 1000),
    }
    await engine.on_fill(event)
    return {"ok": True, "event": event}


# ── 진입점 ──────────────────────────────────
if __name__ == "__main__":
    uvicorn.run("api.main:app", host="0.0.0.0", port=8001, reload=True)
