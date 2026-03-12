"""
Copy Perp FastAPI 백엔드 v1.0
엔드포인트:
  GET  /                          상태
  GET  /health                    헬스체크 (WS 연결 + BTC 가격)
  GET  /markets[?symbol=BTC]      마켓 목록 (WS 실시간)
  GET  /traders[?mock=true]       트레이더 리더보드
  POST /traders                   트레이더 등록 + 모니터 시작
  GET  /traders/{addr}            트레이더 상세
  GET  /traders/{addr}/trades     트레이더 체결 이력
  GET  /traders/{addr}/followers  팔로워 목록
  POST /follow                    팔로우 시작
  DELETE /follow/{addr}           팔로우 중지
  GET  /trades[?limit=50]         복사 거래 내역
  GET  /stats                     플랫폼 통계
  GET  /referral/{addr}           레퍼럴 링크 + 포인트
  POST /referral/track            레퍼럴 추적
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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional

from db.database import init_db, add_trader, add_follower, get_followers, get_leaderboard
from pacifica.client import PacificaClient
from core.copy_engine import CopyEngine
from core.position_monitor import PositionMonitor
from core.stats import get_platform_stats
from fuul.referral import FuulReferral
from api.routers.traders import router as traders_router

app = FastAPI(title="Copy Perp API", version="1.0.0", docs_url="/docs")

# CORS (프론트엔드 연동)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 라우터 등록
app.include_router(traders_router)

# ── 전역 상태 ─────────────────────────────────────────
_db = None
_engine = None
_monitors: dict[str, PositionMonitor] = {}
_price_cache: dict = {}
_fuul = FuulReferral()


async def get_db():
    global _db
    if _db is None:
        _db = await init_db()
    return _db


# ── WS 가격 스트림 ────────────────────────────────────
async def _price_stream_loop():
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


# ── 시작/종료 ─────────────────────────────────────────
@app.on_event("startup")
async def startup():
    global _db, _engine
    _db = await init_db()
    _engine = CopyEngine(_db)
    asyncio.create_task(_price_stream_loop())


# ── 요청 모델 ─────────────────────────────────────────
class FollowRequest(BaseModel):
    follower_address: str
    trader_address: str
    copy_ratio: float = 0.5
    max_position_usdc: float = 50.0
    referrer_address: Optional[str] = None

class UnfollowRequest(BaseModel):
    follower_address: str

class ReferralTrackRequest(BaseModel):
    referrer: str
    referee: str


# ── 기본 엔드포인트 ───────────────────────────────────
@app.get("/")
def root():
    return {"status": "ok", "service": "Copy Perp", "version": "1.0.0", "docs": "/docs"}


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
        "symbols_cached": len(_price_cache),
    }


@app.get("/markets")
def get_markets(symbol: Optional[str] = None):
    if symbol:
        data = _price_cache.get(symbol.upper())
        if not data:
            raise HTTPException(404, f"{symbol} not found in cache")
        return {"data": data}
    # 펀딩비 기준 정렬 (절댓값 높은 것 = 아비트라지 기회)
    items = sorted(_price_cache.values(), key=lambda x: abs(float(x.get("funding", 0))), reverse=True)
    return {"data": items, "count": len(items)}


# ── 팔로우 ────────────────────────────────────────────
@app.post("/follow")
async def follow_trader(body: FollowRequest, background_tasks: BackgroundTasks):
    db = await get_db()
    await add_trader(db, body.trader_address)
    await add_follower(
        db,
        address=body.follower_address,
        trader_address=body.trader_address,
        copy_ratio=body.copy_ratio,
        max_position_usdc=body.max_position_usdc,
    )

    # 레퍼럴 추적
    if body.referrer_address:
        await _fuul.track_referral(body.referrer_address, body.follower_address)

    # 모니터 시작
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
        "monitoring": True,
    }


@app.delete("/follow/{trader_address}")
async def unfollow_trader(trader_address: str, body: UnfollowRequest):
    db = await get_db()
    await db.execute(
        "UPDATE followers SET active=0 WHERE address=? AND trader_address=?",
        (body.follower_address, trader_address)
    )
    await db.commit()

    followers = await get_followers(db, trader_address)
    if not followers and trader_address in _monitors:
        await _monitors[trader_address].stop()
        del _monitors[trader_address]

    return {"status": "ok", "unfollowed": trader_address}


# ── 거래 내역 ─────────────────────────────────────────
@app.get("/trades")
async def list_trades(limit: int = 50, follower: Optional[str] = None, trader: Optional[str] = None):
    db = await get_db()
    if follower:
        q, p = "SELECT * FROM copy_trades WHERE follower_address=? ORDER BY created_at DESC LIMIT ?", (follower, limit)
    elif trader:
        q, p = "SELECT * FROM copy_trades WHERE trader_address=? ORDER BY created_at DESC LIMIT ?", (trader, limit)
    else:
        q, p = "SELECT * FROM copy_trades ORDER BY created_at DESC LIMIT ?", (limit,)

    async with db.execute(q, p) as cur:
        rows = await cur.fetchall()
    return {"data": [dict(r) for r in rows], "count": len(rows)}


# ── 통계 ──────────────────────────────────────────────
@app.get("/stats")
async def get_stats():
    db = await get_db()
    stats = await get_platform_stats(db)
    stats["ws_symbols"] = len(_price_cache)
    stats["active_monitors"] = len(_monitors)
    return stats


# ── 레퍼럴 ────────────────────────────────────────────
@app.get("/referral/{address}")
def get_referral(address: str):
    link = _fuul.generate_referral_link(address)
    points = _fuul.get_points(address)
    return {"address": address, "referral_link": link, "points": points}


@app.post("/referral/track")
async def track_referral(body: ReferralTrackRequest):
    result = await _fuul.track_referral(body.referrer, body.referee)
    return result


@app.get("/referral/leaderboard")
def referral_leaderboard(limit: int = 10):
    return {"data": _fuul.get_leaderboard(limit)}


# ── 프론트엔드 정적 파일 (마지막에 마운트) ────────────
frontend_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
if os.path.exists(frontend_path):
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")
