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
from core.position_monitor import PositionMonitor, RestPositionMonitor
from core.stats import get_platform_stats
from fuul.referral import FuulReferral
from api.routers.traders import router as traders_router
from api.routers.builder import router as builder_router

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
app.include_router(builder_router)

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
    WS_URL = os.getenv("PACIFICA_WS_URL", "wss://ws.pacifica.fi/ws")
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
async def _rest_price_poll_loop():
    """WS 차단 시 REST 폴링으로 가격 캐시 유지 (30초 주기)"""
    from pacifica.client import PacificaClient
    import asyncio
    client = PacificaClient()
    while True:
        try:
            prices = await asyncio.get_event_loop().run_in_executor(None, client.get_prices)
            if isinstance(prices, list):
                for item in prices:
                    sym = item.get("symbol")
                    if sym:
                        # REST 응답 필드를 WS 포맷으로 정규화
                        _price_cache[sym] = {
                            "symbol": sym,
                            "mark": item.get("mark", "0"),
                            "oracle": item.get("oracle", "0"),
                            "funding": item.get("funding", "0"),
                            "open_interest": item.get("open_interest", "0"),
                            "volume_24h": item.get("volume_24h", "0"),
                            "mid": item.get("mid", "0"),
                        }
        except Exception as e:
            pass
        await asyncio.sleep(30)


async def _sync_leaderboard_loop():
    """Pacifica 실제 리더보드 주기적 동기화 (DB 업서트)"""
    from pacifica.client import PacificaClient
    client = PacificaClient()
    while True:
        try:
            lb = await asyncio.get_event_loop().run_in_executor(
                None, lambda: client.get_leaderboard(100)
            )
            for t in lb:
                addr = t.get("address", "")
                if not addr:
                    continue
                pnl_all = float(t.get("pnl_all_time", 0) or 0)
                pnl_30d = float(t.get("pnl_30d", 0) or 0)
                pnl_7d  = float(t.get("pnl_7d", 0) or 0)
                pnl_1d  = float(t.get("pnl_1d", 0) or 0)
                equity  = float(t.get("equity_current", 0) or 0)
                # 복합 점수: roi_30d*0.6 + roi_7d*0.3 + 1d 보너스
                score = (pnl_30d/equity*0.6 + pnl_7d/equity*0.3 + (0.1 if pnl_1d > 0 else 0)) if equity > 0 else 0
                await _db.execute(
                    """INSERT OR REPLACE INTO traders
                       (address, alias, total_pnl, win_rate, followers,
                        pnl_1d, pnl_7d, pnl_30d, pnl_all_time, equity,
                        volume_7d, volume_30d, oi_current, active, last_synced)
                       VALUES (?,?,?,?,0, ?,?,?,?,?,?,?,?,1,strftime('%s','now'))""",
                    (
                        addr,
                        t.get("username") or addr[:8],
                        pnl_all,
                        score * 100,  # win_rate 컬럼에 composite score 저장
                        pnl_1d, pnl_7d, pnl_30d, pnl_all, equity,
                        float(t.get("volume_7d", 0) or 0),
                        float(t.get("volume_30d", 0) or 0),
                        float(t.get("oi_current", 0) or 0),
                    )
                )
            await _db.commit()
        except Exception:
            pass
        await asyncio.sleep(60)  # 1분마다 갱신


@app.on_event("startup")
async def startup():
    global _db, _engine
    _db = await init_db()
    _engine = CopyEngine(_db)
    asyncio.create_task(_price_stream_loop())
    # WS 차단 환경 대비 REST 폴링 폴백
    asyncio.create_task(_rest_price_poll_loop())
    # 실제 Pacifica 리더보드 동기화
    asyncio.create_task(_sync_leaderboard_loop())


# ── 요청 모델 ─────────────────────────────────────────
BUILDER_CODE = os.getenv("BUILDER_CODE", "noivan")

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


# ── 시그널 ────────────────────────────────────────────
@app.get("/signals")
def get_signals(top_n: int = 5):
    """실시간 시그널 — 펀딩비 극단 + Oracle-Mark 괴리"""
    items = list(_price_cache.values())
    funding_top = sorted(items, key=lambda x: abs(float(x.get("funding", 0))), reverse=True)[:top_n]
    divergence_top = sorted(
        [m for m in items if float(m.get("oracle", 0)) > 0],
        key=lambda x: abs(float(x.get("mark", 0)) - float(x.get("oracle", 0))) / float(x.get("oracle", 1)),
        reverse=True
    )[:top_n]
    return {
        "funding_extremes": funding_top,
        "oracle_mark_divergence": divergence_top,
        "source": "live" if _price_cache else "empty",
    }


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
        monitor = RestPositionMonitor(body.trader_address, _engine.on_fill)  # WS 차단 환경 → REST 폴링
        _monitors[body.trader_address] = monitor
        background_tasks.add_task(monitor.start)

    return {
        "status": "ok",
        "follower": body.follower_address,
        "trader": body.trader_address,
        "copy_ratio": body.copy_ratio,
        "max_position_usdc": body.max_position_usdc,
        "builder_code": BUILDER_CODE,
        "monitoring": True,
        "note": f"Builder Code '{BUILDER_CODE}' — 프론트에서 유저 서명 승인 필요",
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
@app.get("/fuul/leaderboard")
def referral_leaderboard(limit: int = 10):
    """레퍼럴 포인트 리더보드"""
    return {"data": _fuul.get_leaderboard(limit)}


@app.post("/fuul/track")
async def track_referral(body: ReferralTrackRequest):
    """레퍼럴 추적"""
    result = await _fuul.track_referral(body.referrer, body.referee)
    return result


@app.get("/referral/{address}")
def get_referral(address: str):
    """개별 레퍼럴 링크 + 포인트"""
    link = _fuul.generate_referral_link(address)
    points = _fuul.get_points(address)
    return {"address": address, "referral_link": link, "points": points}


# ── 프론트엔드 정적 파일 (마지막에 마운트) ────────────
frontend_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
if os.path.exists(frontend_path):
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")
