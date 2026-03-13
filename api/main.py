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
import logging
import os
import sys
import warnings

logger = logging.getLogger(__name__)
warnings.filterwarnings("ignore")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
# .env를 명시적 경로로 로드 (uvicorn 실행 위치와 무관하게 동작)
_env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env')
load_dotenv(_env_path, override=True)

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
from api.routers.followers import router as followers_router

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
app.include_router(followers_router)

# ── 전역 상태 ─────────────────────────────────────────
_db = None
_engine = None
_monitors: dict[str, PositionMonitor] = {}
# _price_cache는 core.data_collector에서 관리
from core.data_collector import get_price_cache as _get_pc, is_connected as _dc_connected, start_polling as _dc_start
_fuul = FuulReferral()


async def get_db():
    global _db
    if _db is None:
        _db = await init_db()
    return _db


# ── 가격 데이터 수집 — DataCollector REST 폴링 (WS 완전 대체) ──────────────
# WS는 HMG 웹필터에서 차단됨 (CloudFront WS도 502)
# core/data_collector.py — CF SNI GET 방식 30초 주기 폴링으로 완전 교체


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
                # INSERT OR IGNORE: 신규만 삽입, 기존 win_rate/win_count 보존
                await _db.execute(
                    """INSERT OR IGNORE INTO traders
                       (address, alias, total_pnl, followers,
                        pnl_1d, pnl_7d, pnl_30d, pnl_all_time, equity,
                        volume_7d, volume_30d, oi_current, active, last_synced)
                       VALUES (?,?,?,0, ?,?,?,?,?,?,?,?,1,strftime('%s','now'))""",
                    (
                        addr,
                        t.get("username") or addr[:8],
                        pnl_all,
                        pnl_1d, pnl_7d, pnl_30d, pnl_all, equity,
                        float(t.get("volume_7d", 0) or 0),
                        float(t.get("volume_30d", 0) or 0),
                        float(t.get("oi_current", 0) or 0),
                    )
                )
                # 기존 행은 PnL/equity 수치만 업데이트 (win_rate/win_count 보존)
                await _db.execute(
                    """UPDATE traders SET
                       alias=COALESCE(?,alias), total_pnl=?, pnl_1d=?, pnl_7d=?,
                       pnl_30d=?, pnl_all_time=?, equity=?, volume_7d=?,
                       volume_30d=?, oi_current=?, active=1,
                       last_synced=strftime('%s','now')
                       WHERE address=?""",
                    (
                        t.get("username") or None,
                        pnl_all, pnl_1d, pnl_7d, pnl_30d, pnl_all, equity,
                        float(t.get("volume_7d", 0) or 0),
                        float(t.get("volume_30d", 0) or 0),
                        float(t.get("oi_current", 0) or 0),
                        addr,
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
    # DataCollector REST 폴링 (WS 완전 대체 — HMG 차단)
    asyncio.create_task(_dc_start(interval=30))
    # 실제 Pacifica 리더보드 동기화
    asyncio.create_task(_sync_leaderboard_loop())
    # QA팀 추천 트레이더 자동 모니터링 시작
    asyncio.create_task(_auto_monitor_top_traders())


# QA팀 추천 TOP5 트레이더 (복합 스코어 + 백테스트 ROI 기준)
# QA 추천 + 리서치팀 Tier A 통합 모니터링 목록 (중복 제거)
TOP_TRADERS = list({
    "EcX5xSDT45Nvhi2gMTjTnhF3KT2w4sPF54esEZS3hwZu",  # [Tier A w0.30] ROI82.5%
    "4UBH19qUbXEaqyz9fKrFHuvj8BPMoM87H71s1YPKyGYq",   # [Tier A w0.20] ROI58.4% Win100%
    "7C3sXQ6KvXJLkYGwzjNy2BHpkfEnRHzzfVAgUS64CDEd",   # [Tier A w0.20] ROI57.7%
    "7gV81bz99MUBVb2aLYxW7MG1RMDdRdJYTPyC2syjba8y",   # [Tier A w0.15] ROI51.6%
    "3rXoG6i55P7D1Q3tYsB7Unds8nBtKh7vH5VUyMDpWkSe",   # [Tier A w0.15] ROI47.5%
    "5C9GKLrKFUvLWZEbMZQC5mtkTdKxuUhCzVCXZQH4FmCw",  # [QA] ROI+24% MaxDD0.1%
    "EYhhf8u9M6kN9tCRVgd2Jki9fJm3XzJRnTF9k5eBC1q1",  # [QA] ROI+10% PF1000
    "A6VY4ZBUohgSLkwMuDwDvAnzgiXFB1eTDzaixyitPJep",   # [QA] Win92% PnL$166k
})

async def _auto_monitor_top_traders():
    """QA 추천 TOP5 트레이더 자동 포지션 모니터링"""
    global _monitors, _engine
    await asyncio.sleep(3)  # 엔진 초기화 대기
    for addr in TOP_TRADERS:
        if addr not in _monitors:
            try:
                monitor = RestPositionMonitor(addr, _engine.on_fill)
                _monitors[addr] = monitor
                asyncio.create_task(monitor.start())
                logger.info(f"[Auto] 모니터링 시작: {addr[:16]}...")
            except Exception as e:
                logger.warning(f"[Auto] 모니터 시작 실패 {addr[:12]}: {e}")
    logger.info(f"[Auto] TOP5 트레이더 모니터링 완료: {len(_monitors)}개")


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
    btc = _get_pc().get("BTC", {})
    return {
        "status": "ok",
        "data_connected": _dc_connected(),           # REST 폴링 연결 상태
        "ws_connected":   _dc_connected(),           # 하위 호환 (WS → REST 전환)
        "data_source":    "rest_poll",               # 데이터 소스 명시
        "btc_mark":    btc.get("mark"),
        "btc_funding": btc.get("funding"),
        "btc_oi":      btc.get("open_interest"),
        "active_monitors": len(_monitors),
        "symbols_cached":  len(_get_pc()),
    }


@app.get("/markets")
def get_markets(symbol: Optional[str] = None):
    if symbol:
        data = _get_pc().get(symbol.upper())
        if not data:
            raise HTTPException(404, f"{symbol} not found in cache")
        return {"data": data}
    # 펀딩비 기준 정렬 (절댓값 높은 것 = 아비트라지 기회)
    items = sorted(_get_pc().values(), key=lambda x: abs(float(x.get("funding", 0))), reverse=True)
    return {"data": items, "count": len(items)}


# ── 시그널 ────────────────────────────────────────────
@app.get("/signals")
def get_signals(top_n: int = 5):
    """실시간 시그널 — 펀딩비 극단 + Oracle-Mark 괴리"""
    items = list(_get_pc().values())
    funding_top = sorted(items, key=lambda x: abs(float(x.get("funding", 0))), reverse=True)[:top_n]
    divergence_top = sorted(
        [m for m in items if float(m.get("oracle", 0)) > 0],
        key=lambda x: abs(float(x.get("mark", 0)) - float(x.get("oracle", 0))) / float(x.get("oracle", 1)),
        reverse=True
    )[:top_n]
    return {
        "funding_extremes": funding_top,
        "oracle_mark_divergence": divergence_top,
        "source": "live" if _get_pc() else "empty",
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
    stats["ws_symbols"] = len(_get_pc())
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
