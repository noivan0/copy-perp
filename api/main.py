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
import time as _time_module
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
from pacifica.client import PacificaClient, _CF_HOST, _PACIFICA_HOST
from core.copy_engine import CopyEngine
from core.position_monitor import PositionMonitor, RestPositionMonitor
from core.stats import get_platform_stats
from fuul.referral import FuulReferral
from api.routers.traders import router as traders_router
from api.routers.builder import router as builder_router
from api.routers.followers import router as followers_router
from api.routers.ranked import router as ranked_router
from api.routers.portfolio import router as portfolio_router
from core.alerting import get_alert_manager

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app_):
    """FastAPI lifespan — startup + graceful shutdown"""
    # ── Startup ──────────────────────────────────
    global _db, _engine
    _db = await init_db()
    _engine = CopyEngine(_db)

    _network = os.getenv("NETWORK", "testnet")
    _rest_url = os.getenv("PACIFICA_REST_URL", "")
    _db_path  = os.getenv("DB_PATH", "copy_perp.db")
    logger.info(f"🌐 NETWORK={_network} | REST={_rest_url} | DB={_db_path}")
    if _network == "mainnet":
        logger.info("🚀 MAINNET MODE: api.pacifica.fi 직접 접근")
    else:
        logger.info("🧪 TESTNET MODE: CloudFront SNI 우회")

    asyncio.create_task(_dc_start(interval=30))
    asyncio.create_task(_sync_leaderboard_loop())
    asyncio.create_task(_restore_monitors_from_db())
    asyncio.create_task(_auto_monitor_top_traders())
    asyncio.create_task(_winrate_refresh_loop())

    get_alert_manager().server_started(_network, 0)
    logger.info("✅ Copy Perp 서버 시작 완료")

    yield  # ← 서버 실행 구간

    # ── Shutdown ─────────────────────────────────
    logger.info("🛑 Graceful shutdown 시작...")
    # 모든 모니터 중지
    for addr, monitor in list(_monitors.items()):
        try:
            monitor._running = False
            logger.info(f"  모니터 중지: {addr[:16]}...")
        except Exception:
            pass
    # DB 연결 닫기
    if _db:
        await _db.close()
        logger.info("  DB 연결 닫힘")
    logger.info("✅ Graceful shutdown 완료")

app = FastAPI(title="Copy Perp API", version="1.0.0", docs_url="/docs", lifespan=lifespan)

# CORS (프론트엔드 연동)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 라우터 등록
app.include_router(ranked_router)   # /traders/ranked — traders보다 먼저 (경로 충돌 방지)
app.include_router(portfolio_router)
app.include_router(traders_router)
app.include_router(builder_router)
app.include_router(followers_router)

# ── 전역 상태 ─────────────────────────────────────────
_db = None
_engine = None
_monitors: dict[str, PositionMonitor] = {}
_start_time: float = _time_module.time()  # 서버 기동 시각
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
    await asyncio.sleep(10)  # 서버 완전 기동 후 시작
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


async def _winrate_refresh_loop():
    """win_rate 자동 갱신 — 6시간마다 Tier1 트레이더 trades/history 재수집"""
    import ssl as _ssl
    await asyncio.sleep(60)  # 기동 후 1분 대기
    while True:
        try:
            db = await get_db()
            async with db.execute(
                "SELECT address, alias FROM traders WHERE active=1 ORDER BY pnl_all_time DESC LIMIT 12"
            ) as cur:
                top_traders = await cur.fetchall()

            for row in top_traders:
                addr = row[0]
                try:
                    import json as _j, socket as _sock
                    ctx = _ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=_ssl.CERT_NONE
                    s = _sock.create_connection((_CF_HOST, 443), timeout=12)
                    ss = ctx.wrap_socket(s, server_hostname=_CF_HOST)
                    req = (f"GET /api/v1/trades/history?account={addr}&limit=100 HTTP/1.1\r\n"
                           f"Host: {_PACIFICA_HOST}\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n")
                    ss.sendall(req.encode()); data = b''
                    ss.settimeout(12)
                    try:
                        while True:
                            c = ss.recv(16384)
                            if not c: break
                            data += c
                    except Exception: pass
                    ss.close()
                    body = data.split(b'\r\n\r\n', 1)[1] if b'\r\n\r\n' in data else data
                    result = _j.loads(body.decode('utf-8', 'ignore'))
                    trades = result.get('data', []) if isinstance(result, dict) else []
                    closes = [t for t in trades if 'close' in t.get('side', '')]
                    wins = sum(1 for t in closes if float(t.get('pnl', 0) or 0) > 0)
                    losses = len(closes) - wins
                    total = wins + losses
                    wr = wins / total if total > 0 else 0.0
                    await db.execute(
                        "UPDATE traders SET win_rate=?, win_count=?, lose_count=? WHERE address=?",
                        (wr, wins, losses, addr)
                    )
                    await asyncio.sleep(0.3)  # rate limit
                except Exception as e:
                    logger.debug(f"win_rate 갱신 실패 {addr[:12]}: {e}")

            await db.commit()
            logger.info(f"[WinRate] {len(top_traders)}명 갱신 완료")
        except Exception as e:
            logger.warning(f"[WinRate] 갱신 루프 오류: {e}")

        await asyncio.sleep(6 * 3600)  # 6시간마다


# startup 이벤트는 lifespan으로 대체됨 (위 lifespan 함수 참조)
# 하위 호환을 위해 deprecated 이벤트 유지 (lifespan과 중복 실행 방지)
@app.on_event("startup")
async def _startup_compat():
    """lifespan 미지원 환경 대비 deprecated fallback — 이미 lifespan에서 처리됨"""
    pass


async def _restore_monitors_from_db():
    """서버 재기동 후 DB의 active=1 팔로워가 팔로우하는 트레이더 monitor 자동 복원"""
    global _monitors, _engine
    await asyncio.sleep(2)  # 엔진 초기화 대기
    try:
        async with _db.execute(
            "SELECT DISTINCT trader_address FROM followers WHERE active=1 AND trader_address IS NOT NULL"
        ) as cur:
            rows = await cur.fetchall()
        restored = 0
        for row in rows:
            trader_addr = row[0]
            if not trader_addr:
                continue
            if trader_addr not in _monitors:
                try:
                    monitor = RestPositionMonitor(trader_addr, _engine.on_fill)
                    _monitors[trader_addr] = monitor
                    asyncio.create_task(monitor.start())
                    restored += 1
                    logger.info(f"[Restore] monitor 복원: {trader_addr[:16]}...")
                except Exception as e:
                    logger.warning(f"[Restore] monitor 복원 실패 {trader_addr[:12]}: {e}")
        logger.info(f"[Restore] DB에서 {restored}개 monitor 복원 완료")
    except Exception as e:
        logger.warning(f"[Restore] monitor 자동 복원 오류: {e}")


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


@app.get("/leaderboard")
async def leaderboard_alias(limit: int = 20):
    """/traders의 alias — 프론트 호환성"""
    from db.database import get_leaderboard as _get_lb
    from pacifica.client import PacificaClient
    _pac = PacificaClient()
    try:
        real_lb = await asyncio.get_event_loop().run_in_executor(None, lambda: _pac.get_leaderboard(limit=limit))
        if isinstance(real_lb, list) and real_lb:
            return {"data": real_lb, "count": len(real_lb)}
    except Exception:
        pass
    if _db:
        leaders = await _get_lb(_db, limit)
        return {"data": [dict(r) for r in leaders], "count": len(leaders)}
    return {"data": [], "count": 0}


@app.get("/health")
def health():
    import os as _os
    btc = _get_pc().get("BTC", {})

    # monitors_detail: 각 monitor의 trader 주소 + 마지막 폴링 시각
    monitors_detail = []
    for addr, mon in _monitors.items():
        last_poll = getattr(mon, "_last_poll_time", None)
        monitors_detail.append({
            "trader": addr,
            "last_poll_at": last_poll,
            "fail_count": getattr(mon, "_fail_count", 0),
        })

    # DB 파일 크기
    db_path = os.getenv("DB_PATH", "copy_perp.db")
    try:
        db_size_bytes = _os.path.getsize(db_path)
    except Exception:
        db_size_bytes = -1

    # mainnet/testnet 트레이더 수 집계 (sync sqlite3)
    network_env = os.getenv("NETWORK", "testnet")
    mainnet_traders_count: Optional[int] = None
    testnet_traders_count: Optional[int] = None
    try:
        import sqlite3 as _sqlite3
        _db_path2 = os.getenv("DB_PATH", "copy_perp.db")
        with _sqlite3.connect(_db_path2) as _sc:
            _row = _sc.execute("SELECT COUNT(*) FROM traders WHERE active=1").fetchone()
            active_cnt = _row[0] if _row else 0
        if network_env == "mainnet":
            mainnet_traders_count = active_cnt
        else:
            testnet_traders_count = active_cnt
    except Exception:
        pass

    return {
        "status": "ok",
        "network":        network_env,               # mainnet / testnet
        "data_connected": _dc_connected(),           # REST 폴링 연결 상태
        "ws_connected":   _dc_connected(),           # 하위 호환 (WS → REST 전환)
        "data_source":    "rest_poll",               # 데이터 소스 명시
        "btc_mark":    btc.get("mark"),
        "btc_funding": btc.get("funding"),
        "btc_oi":      btc.get("open_interest"),
        "active_monitors": len(_monitors),
        "symbols_cached":  len(_get_pc()),
        "monitors_detail": monitors_detail,          # 각 monitor 상세
        "uptime_seconds":  round(_time_module.time() - _start_time, 1),  # 서버 기동 후 경과 시간
        "db_size_bytes":   db_size_bytes,            # DB 파일 크기
        "mainnet_traders": mainnet_traders_count,    # mainnet 활성 트레이더 수
        "testnet_traders": testnet_traders_count,    # testnet 활성 트레이더 수
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
async def list_trades(
    limit: int = 50,
    follower: Optional[str] = None,
    trader:   Optional[str] = None,
    status:   Optional[str] = None,   # filled | pending | failed
):
    """Copy Trade 내역 조회 (필터: follower, trader, status)"""
    db = await get_db()
    conditions, params = [], []
    if follower:
        conditions.append("follower_address=?"); params.append(follower)
    if trader:
        conditions.append("trader_address=?");   params.append(trader)
    if status:
        conditions.append("status=?");           params.append(status)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)
    async with db.execute(
        f"SELECT * FROM copy_trades {where} ORDER BY created_at DESC LIMIT ?", params
    ) as cur:
        rows = await cur.fetchall()
    data = [dict(r) for r in rows]
    filled  = [r for r in data if r.get("status") == "filled"]
    total_vol = sum(float(r.get("amount",0) or 0) * float(r.get("price",0) or 0) for r in filled)
    total_pnl = sum(float(r.get("pnl",0) or 0) for r in filled)
    return {
        "data": data,
        "count": len(data),
        "summary": {
            "filled": len(filled),
            "total_volume_usdc": round(total_vol, 2),
            "total_pnl_usdc": round(total_pnl, 4),
        },
    }


# ── 통계 ──────────────────────────────────────────────
@app.get("/stats/overview")
@app.get("/stats")
async def get_stats():
    db = await get_db()
    try:
        stats = await get_platform_stats(db)
    except Exception:
        import sqlite3 as _sq3, aiosqlite as _aio
        # fallback: 직접 DB 조회
        async with db.execute("SELECT COUNT(*) FROM traders WHERE active=1") as cur:
            t_count = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM followers WHERE active=1") as cur:
            f_count = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM copy_trades WHERE status='filled'") as cur:
            trade_count = (await cur.fetchone())[0]
        stats = {
            "active_traders": t_count,
            "active_followers": f_count,
            "total_trades_filled": trade_count,
            "total_pnl_usdc": 0.0,
            "total_volume_usdc": 0.0,
        }
    stats["ws_symbols"] = len(_get_pc())
    stats["active_monitors"] = len(_monitors)
    return stats


# ── 메트릭 / 이벤트 로그 ──────────────────────────────
@app.get("/metrics")
async def get_metrics():
    """Prometheus 텍스트 형식 메트릭"""
    from fastapi.responses import PlainTextResponse
    db = await get_db()
    s = await get_platform_stats(db)
    btc = _get_pc().get("BTC", {})
    lines = [
        f"copy_perp_active_traders {s.get('active_traders', 0)}",
        f"copy_perp_active_followers {s.get('active_followers', 0)}",
        f"copy_perp_copy_trades_total {s.get('total_trades_filled', 0)}",
        f"copy_perp_volume_usdc {s.get('total_volume_usdc', 0)}",
        f"copy_perp_monitors_active {len(_monitors)}",
        f"copy_perp_btc_price {float(btc.get('mark', 0))}",
        f"copy_perp_symbols_cached {len(_get_pc())}",
        f'copy_perp_network{{network="{os.getenv("NETWORK","testnet")}"}} 1',
    ]
    # 알림 에러 카운트
    am = get_alert_manager()
    summary = am.get_error_summary()
    for k, v in summary.get("total_error_counts", {}).items():
        lines.append(f'copy_perp_order_errors{{follower="{k}"}} {v}')
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain")


@app.get("/events")
def get_events(limit: int = 50, level: Optional[str] = None):
    """최근 시스템 이벤트 로그 (주문 실패, 연결 끊김 등)"""
    am = get_alert_manager()
    return {
        "data": am.get_recent_events(limit=limit, level=level),
        "summary": am.get_error_summary(),
    }


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

@app.get("/health/detailed")
async def health_detailed():
    """상세 헬스 체크 — 모니터 상태, DB, 환경"""
    import os, time
    import core.data_collector as _dc_mod
    from core.data_collector import get_price_cache
    
    db = await get_db()
    try:
        async with db.execute("SELECT COUNT(*) FROM traders WHERE active=1") as c:
            trader_count = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM followers WHERE active=1") as c:
            follower_count = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM copy_trades WHERE status='filled'") as c:
            filled_count = (await c.fetchone())[0]
        async with db.execute("SELECT COALESCE(SUM(CASE WHEN pnl IS NOT NULL THEN pnl ELSE 0 END),0) FROM copy_trades WHERE status='filled'") as c:
            total_pnl = (await c.fetchone())[0]
        db_ok = True
    except Exception as e:
        db_ok = False
        trader_count = follower_count = filled_count = 0
        total_pnl = 0

    # 모니터 상태 상세
    now = time.time()
    monitors_detail = []
    for addr, mon in _monitors.items():
        lpt = getattr(mon, "_last_poll_time", None)
        fc  = getattr(mon, "_fail_count", 0)
        monitors_detail.append({
            "trader": addr,
            "running": getattr(mon, "_running", True),
            "last_poll_time": lpt,
            "last_poll_ago_sec": round(now - lpt, 1) if lpt else None,
            "fail_count": fc,
        })

    # uptime
    uptime_sec = round(now - _start_time, 1)

    # DB 파일 크기
    db_path = os.getenv("DB_PATH", "copy_perp.db")
    try:
        db_size = os.path.getsize(db_path)
    except Exception:
        db_size = None

    # 데이터 수신 상태 (모듈 변수 직접 참조로 최신값 보장)
    last_poll = _dc_mod._last_poll_ts

    return {
        "status": "ok",
        "network": os.getenv("NETWORK", "testnet"),
        "uptime_seconds": uptime_sec,
        "db": {
            "ok": db_ok,
            "size_bytes": db_size,
            "traders": trader_count,
            "followers": follower_count,
            "filled_trades": filled_count,
            "total_pnl_usdc": round(total_pnl, 4),
        },
        "data_collector": {
            "connected": _dc_connected(),
            "symbols_cached": len(get_price_cache()),
            "source": "rest_poll",
            "last_poll_ago_sec": round(now - last_poll, 1) if last_poll else None,
        },
        "monitors": {
            "count": len(_monitors),
            "detail": monitors_detail,
        },
        "environment": {
            "builder_code": BUILDER_CODE,
            "network": os.getenv("NETWORK", "testnet"),
            "rest_url": os.getenv("PACIFICA_REST_URL", "auto"),
        }
    }






# ── Builder Code 승인 (프론트에서 서명) ──────────────────
# Builder Code 엔드포인트는 api/routers/builder.py (builder_router)에서 관리
# /builder/approve, /builder/check, /builder/stats, /builder/trades, /builder/prepare-approval
# 여기에 중복 정의하지 않음

# ── 팔로워 온보딩 (프론트엔드 호환) ─────────────────────
class OnboardRequest(BaseModel):
    follower_address: str
    traders: list  # 트레이더 주소 리스트
    copy_ratio: float = 0.1
    max_position_usdc: float = 50.0
    referrer_address: Optional[str] = None


@app.post("/followers/onboard")
async def onboard_follower(body: OnboardRequest, background_tasks: BackgroundTasks):
    """팔로워 온보딩 — 여러 트레이더를 한번에 팔로우"""
    db = await get_db()
    results = []
    errors = []

    for trader_addr in body.traders:
        try:
            await add_trader(db, trader_addr)
            await add_follower(
                db,
                address=body.follower_address,
                trader_address=trader_addr,
                copy_ratio=body.copy_ratio,
                max_position_usdc=body.max_position_usdc,
            )
            # 모니터 시작
            if trader_addr not in _monitors:
                monitor = RestPositionMonitor(trader_addr, _engine.on_fill)
                _monitors[trader_addr] = monitor
                background_tasks.add_task(monitor.start)
            results.append({"trader": trader_addr, "status": "ok"})
        except Exception as e:
            errors.append({"trader": trader_addr, "error": str(e)})

    # 레퍼럴 추적
    if body.referrer_address and results:
        try:
            await _fuul.track_referral(body.referrer_address, body.follower_address)
        except Exception:
            pass

    return {
        "ok": len(results) > 0,
        "follower": body.follower_address,
        "followed": results,
        "errors": errors,
        "copy_ratio": body.copy_ratio,
        "max_position_usdc": body.max_position_usdc,
        "builder_code": BUILDER_CODE,
        "note": f"Builder Code '{BUILDER_CODE}' 승인 대기 중 — 주문 실행은 가능",
    }


@app.get("/followers/{address}")
async def get_follower_info(address: str):
    """팔로워 정보 조회"""
    db = await get_db()
    from core.stats import get_follower_stats
    stats = await get_follower_stats(db, address)
    link = _fuul.generate_referral_link(address)
    return {
        "address": address,
        "referral_link": link,
        "stats": stats,
    }


# ── 프론트엔드 정적 파일 (마지막에 마운트) ────────────
frontend_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
if os.path.exists(frontend_path):
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")
