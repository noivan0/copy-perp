"""
Copy Perp FastAPI 백엔드
엔드포인트:
  GET  /                   상태 확인
  GET  /health             헬스체크 + BTC 가격
  GET  /markets            마켓 목록
  GET  /traders            등록된 트레이더 리더보드
  POST /traders            트레이더 등록
  GET  /traders/{addr}     트레이더 상세
  POST /follow             팔로우 시작
  DELETE /follow/{addr}    팔로우 중지
  GET  /trades             복사 거래 내역
  GET  /stats              전체 통계
"""
import asyncio
import os
import sys
import warnings
warnings.filterwarnings("ignore")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
from typing import Optional

from db.database import init_db, add_trader, add_follower, get_followers, get_leaderboard
from pacifica.client import PacificaClient
from core.copy_engine import CopyEngine
from core.position_monitor import PositionMonitor

app = FastAPI(title="Copy Perp API", version="1.0.0")

# ── 전역 상태 ─────────────────────────────────────────
_db = None
_engine = None
_monitors: dict[str, PositionMonitor] = {}
_price_cache: dict = {}


async def get_db():
    global _db
    if _db is None:
        _db = await init_db()
    return _db


# ── 시작/종료 ─────────────────────────────────────────
@app.on_event("startup")
async def startup():
    global _db, _engine
    _db = await init_db()
    _engine = CopyEngine(_db)

    # WS 가격 스트림 백그라운드
    asyncio.create_task(_price_stream_loop())


async def _price_stream_loop():
    """WS prices 채널 구독 → _price_cache 갱신"""
    import json, ssl, websockets
    WS_URL = os.getenv("PACIFICA_WS_URL", "wss://test-ws.pacifica.fi/ws")
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    while True:
        try:
            async with websockets.connect(WS_URL, ssl=ssl_ctx, ping_interval=30) as ws:
                await ws.send(json.dumps({"method": "subscribe", "params": {"source": "prices"}}))
                async for raw in ws:
                    data = json.loads(raw)
                    if data.get("channel") == "prices":
                        for item in data.get("data", []):
                            _price_cache[item["symbol"]] = item
        except Exception:
            await asyncio.sleep(3)


# ── 요청 모델 ─────────────────────────────────────────
class TraderRegister(BaseModel):
    address: str
    alias: Optional[str] = ""

class FollowRequest(BaseModel):
    follower_address: str
    trader_address: str
    copy_ratio: float = 0.5
    max_position_usdc: float = 50.0

class UnfollowRequest(BaseModel):
    follower_address: str


# ── 엔드포인트 ────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "ok", "service": "Copy Perp", "version": "1.0.0"}


@app.get("/health")
def health():
    btc = _price_cache.get("BTC", {})
    return {
        "status": "ok",
        "ws_connected": bool(btc),
        "btc_mark": btc.get("mark"),
        "btc_funding": btc.get("funding"),
        "btc_oi": btc.get("open_interest"),
        "active_monitors": len(_monitors),
    }


@app.get("/markets")
def get_markets(symbol: Optional[str] = None):
    if symbol:
        data = _price_cache.get(symbol.upper())
        if not data:
            raise HTTPException(404, f"{symbol} not found")
        return {"data": data}
    return {"data": list(_price_cache.values()), "count": len(_price_cache)}


@app.get("/traders")
async def list_traders(limit: int = 20):
    db = await get_db()
    rows = await get_leaderboard(db, limit)
    return {"data": [dict(r) for r in rows]}


@app.post("/traders")
async def register_trader(body: TraderRegister, background_tasks: BackgroundTasks):
    db = await get_db()
    await add_trader(db, body.address, body.alias)

    # Position Monitor 시작
    if body.address not in _monitors:
        monitor = PositionMonitor(body.address, _engine.on_fill)
        _monitors[body.address] = monitor
        background_tasks.add_task(monitor.start)

    return {"status": "ok", "address": body.address, "monitoring": True}


@app.post("/follow")
async def follow_trader(body: FollowRequest, background_tasks: BackgroundTasks):
    db = await get_db()

    # 트레이더 자동 등록
    await add_trader(db, body.trader_address)
    await add_follower(
        db,
        address=body.follower_address,
        trader_address=body.trader_address,
        copy_ratio=body.copy_ratio,
        max_position_usdc=body.max_position_usdc,
    )

    # 모니터 없으면 시작
    if body.trader_address not in _monitors:
        monitor = PositionMonitor(body.trader_address, _engine.on_fill)
        _monitors[body.trader_address] = monitor
        background_tasks.add_task(monitor.start)

    return {
        "status": "ok",
        "follower": body.follower_address,
        "trader": body.trader_address,
        "copy_ratio": body.copy_ratio,
        "max_position_usdc": body.max_position_usdc,
    }


@app.delete("/follow/{trader_address}")
async def unfollow_trader(trader_address: str, body: UnfollowRequest):
    db = await get_db()
    await db.execute(
        "UPDATE followers SET active=0 WHERE address=? AND trader_address=?",
        (body.follower_address, trader_address)
    )
    await db.commit()

    # 팔로워 없으면 모니터 중지
    followers = await get_followers(db, trader_address)
    if not followers and trader_address in _monitors:
        await _monitors[trader_address].stop()
        del _monitors[trader_address]

    return {"status": "ok", "unfollowed": trader_address}


@app.get("/trades")
async def list_trades(limit: int = 50):
    db = await get_db()
    async with db.execute(
        "SELECT * FROM copy_trades ORDER BY created_at DESC LIMIT ?", (limit,)
    ) as cur:
        rows = await cur.fetchall()
    return {"data": [dict(r) for r in rows], "count": len(rows)}


@app.get("/stats")
async def get_stats():
    db = await get_db()
    async with db.execute("SELECT COUNT(*) as c FROM traders WHERE active=1") as cur:
        traders = (await cur.fetchone())["c"]
    async with db.execute("SELECT COUNT(*) as c FROM followers WHERE active=1") as cur:
        followers = (await cur.fetchone())["c"]
    async with db.execute("SELECT COUNT(*) as c, SUM(pnl) as pnl FROM copy_trades WHERE status='filled'") as cur:
        row = await cur.fetchone()
        trades_filled = row["c"]
        total_pnl = row["pnl"] or 0
    return {
        "active_traders": traders,
        "active_followers": followers,
        "trades_filled": trades_filled,
        "total_pnl_usdc": round(total_pnl, 4),
        "ws_symbols": len(_price_cache),
    }
