"""
Copy Perp FastAPI 백엔드 v1.1 (Production)
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
import json
import logging
import os
APP_VERSION = "1.3.2"  # 단일 버전 상수
import sys
import time as _time_module
import uuid
import warnings


# ── 프로덕션 로깅 설정 ────────────────────────────────────────────────────────
class _JSONFormatter(logging.Formatter):
    """구조화된 JSON 로그 포매터 (프로덕션 환경용)"""
    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        # 민감 정보 마스킹: private_key 패턴 제거
        import re
        msg = re.sub(r'(private_key["\s:=]+)[^\s,"\'}{]+', r'\1[REDACTED]', msg, flags=re.IGNORECASE)
        return json.dumps({
            "ts":    _time_module.strftime("%Y-%m-%dT%H:%M:%SZ", _time_module.gmtime()),
            "level": record.levelname,
            "name":  record.name,
            "msg":   msg,
        }, ensure_ascii=False)


def _setup_logging() -> None:
    """DEBUG=false(프로덕션)이면 JSON 포매터, 개발이면 일반 포매터 적용"""
    log_level = logging.DEBUG if os.getenv("DEBUG", "false").lower() in ("1", "true") else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    if not os.getenv("DEBUG", "false").lower() in ("1", "true"):
        fmt = _JSONFormatter()
        for handler in logging.root.handlers:
            handler.setFormatter(fmt)


_setup_logging()
# ─────────────────────────────────────────────────────────────────────────────

logger = logging.getLogger(__name__)
warnings.filterwarnings("ignore")
# 서드파티 라이브러리 노이즈 억제
logging.getLogger("scrapling").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
# .env를 명시적 경로로 로드 (uvicorn 실행 위치와 무관하게 동작)
_env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env')
load_dotenv(_env_path, override=True)

import aiosqlite
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
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
from api.routers.pnl import router as pnl_router
from api.routers.performance import router as performance_router
from api.routers.tracker import router as tracker_router
from api.routers.presets import router as presets_router
from api.routers.papertrading import router as papertrading_router
from core.alerting import get_alert_manager

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app_):
    """FastAPI lifespan — startup + graceful shutdown"""
    # ── Startup ──────────────────────────────────
    global _db, _engine
    _db = await init_db()
    _engine = CopyEngine(_db)

    # startup에서 필수 환경변수 검증
    REQUIRED_ENVS = {
        "ACCOUNT_ADDRESS": "Pacifica 계정 주소 (Privy 지갑)",
        "AGENT_PRIVATE_KEY": "Agent Key 개인키 (주문 서명)",
        "AGENT_WALLET": "Agent 공개키",
    }
    missing = []
    for key, desc in REQUIRED_ENVS.items():
        if not os.getenv(key):
            missing.append(f"  {key}: {desc}")
    if missing:
        logger.error("🚨 필수 환경변수 미설정:\n" + "\n".join(missing))
        logger.error("→ .env 파일 확인 또는 환경변수 설정 후 재시작")
        # 서버는 기동하되 WARNING으로 남김 (헬스체크에서 degraded 표시)
        os.environ["_ENV_DEGRADED"] = "1"
    else:
        logger.info("✅ 환경변수 검증 완료")

    _network = os.getenv("NETWORK", "testnet")
    _rest_url = os.getenv("PACIFICA_REST_URL", "")
    _db_path  = os.getenv("DB_PATH", "copy_perp.db")
    # IMPROVE 4: DB 영속성 체크 로그
    _db_is_persistent = _db_path.startswith("/var/")
    logger.info(
        f"DB 영속성: {'✅ Render Disk' if _db_is_persistent else '⚠️ 로컬 (재배포 시 초기화)'} | {_db_path}"
    )
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
    logger.info("✅ Copy Perp server started")

    yield  # ← 서버 실행 구간

    # ── Shutdown ─────────────────────────────────
    logger.info("🛑 Graceful shutdown 시작...")
    for addr, monitor in list(_monitors.items()):
        try:
            monitor._running = False
            logger.info(f"  모니터 중지: {addr[:16]}...")
        except Exception as e:
            logger.debug(f"무시된 예외: {e}")
    if _db:
        await _db.close()
        logger.info("  DB 연결 닫힘")
    logger.info("✅ Graceful shutdown 완료")

# DEBUG 모드에서만 /docs, /redoc 노출 (프로덕션 보안)
_DEBUG = os.getenv("DEBUG", "false").lower() in ("1", "true", "yes")
app = FastAPI(
    title="Copy Perp API",
    version=APP_VERSION,
    docs_url="/docs" if _DEBUG else None,
    redoc_url="/redoc" if _DEBUG else None,
    lifespan=lifespan,
)

# ── Request ID 미들웨어 ─────────────────────────────
@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request_id = str(uuid.uuid4())[:8]
    request.state.request_id = request_id
    # 요청 로그
    logger.info(f"[{request_id}] {request.method} {request.url.path}")
    # 응답 시간 측정 (IMPROVE 1)
    _t_start = _time_module.perf_counter()
    response = await call_next(request)
    _elapsed_ms = round((_time_module.perf_counter() - _t_start) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Response-Time"] = f"{_elapsed_ms}ms"   # IMPROVE 1
    response.headers["X-API-Version"] = APP_VERSION              # IMPROVE 3
    # 보안 헤더
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

# CORS — 프로덕션: 실제 도메인만 허용
_DEFAULT_ORIGINS = [
    "http://localhost:8001",
    "http://localhost:3000",
    "https://copy-perp.vercel.app",
    "https://copy-perp-frontend.vercel.app",  # Vercel 프론트엔드
    "https://copy-perp.onrender.com",          # Render 백엔드 자체
]
_env_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
_ALLOWED_ORIGINS = list(dict.fromkeys(_DEFAULT_ORIGINS + _env_origins))

# ── 보안 헤더 미들웨어 ─────────────────────────────────
from starlette.middleware.base import BaseHTTPMiddleware

class CacheControlMiddleware(BaseHTTPMiddleware):
    """읽기 전용 엔드포인트 Cache-Control 헤더 자동 추가"""
    _READ_PREFIXES = ("/traders", "/signals", "/stats", "/portfolio", "/markets")
    _SHORT_PREFIXES = ("/healthz", "/health")

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if request.method == "GET":
            path = request.url.path
            if any(path.startswith(p) for p in self._READ_PREFIXES):
                response.headers.setdefault("Cache-Control", "public, max-age=30, stale-while-revalidate=5")
                response.headers.setdefault("Vary", "Accept-Encoding")
            elif any(path.startswith(p) for p in self._SHORT_PREFIXES):
                response.headers.setdefault("Cache-Control", "public, max-age=10")
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' https:; "
            "style-src 'self' 'unsafe-inline' https:; "
            "connect-src 'self' https:; "
            "img-src 'self' data: https:; "
            "font-src 'self' https:; "
            "frame-ancestors 'none';"
        )
        return response

app.add_middleware(CacheControlMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

# ── 커스텀 CORS 미들웨어 (origin 검증 후 조건부 credentials 헤더) ──────────
# 이유: FastAPI 기본 CORSMiddleware는 allow_credentials=True 시 비허용 origin에도
# Access-Control-Allow-Credentials: true를 반환하는 보안 취약점이 있음.
# 커스텀 미들웨어로 origin 화이트리스트 검증 후 조건부로만 CORS 헤더를 붙임.
class CORSMiddlewareCustom(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        origin = request.headers.get("origin", "")

        # OPTIONS preflight 요청 — 허용된 origin만 200 응답
        if request.method == "OPTIONS" and origin in _ALLOWED_ORIGINS:
            from starlette.responses import Response as _Resp
            return _Resp(
                status_code=200,
                headers={
                    "Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Credentials": "true",
                    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Privy-Token",
                    "Access-Control-Max-Age": "600",
                    "Vary": "Origin",
                }
            )

        response = await call_next(request)

        # 허용된 origin에만 CORS 헤더 추가
        if origin in _ALLOWED_ORIGINS:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Privy-Token"
            response.headers["Vary"] = "Origin"

        return response

app.add_middleware(CORSMiddlewareCustom)

# ── 전역 에러 핸들러 ────────────────────────────────
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    req_id = getattr(request.state, "request_id", "??")
    errors = exc.errors()
    first = errors[0] if errors else {}
    msg = first.get("msg", "Validation failed").replace("Value error, ", "")
    field = " → ".join(str(x) for x in first.get("loc", []) if x != "body")
    return JSONResponse(
        status_code=400,
        content={
            "error": msg,
            "code": "VALIDATION_ERROR",
            "field": field or None,
            "request_id": req_id,
        },
    )


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    req_id = getattr(request.state, "request_id", "??")
    return JSONResponse(
        status_code=400,
        content={"error": str(exc), "code": "VALIDATION_ERROR", "request_id": req_id},
    )

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    req_id = getattr(request.state, "request_id", "??")
    logger.error(f"[{req_id}] Unhandled error [{request.method} {request.url.path}]: {exc}", exc_info=True)
    is_debug = os.getenv("DEBUG", "false").lower() in ("1", "true")
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "code": "INTERNAL_SERVER_ERROR",
            "request_id": req_id,
            **({"detail": str(exc)} if is_debug else {}),
        },
    )

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    req_id = getattr(request.state, "request_id", "??")
    # 이미 dict 형식이면 그대로, 아니면 표준 형식으로 변환
    if isinstance(exc.detail, dict):
        content = {**exc.detail, "request_id": req_id}
    else:
        content = {
            "error": str(exc.detail),
            "code": _status_to_code(exc.status_code),
            "request_id": req_id,
        }
    # exc.headers (e.g. Retry-After) 반드시 전달 — 누락 시 rate limit 헤더 소실
    return JSONResponse(
        status_code=exc.status_code,
        content=content,
        headers=dict(exc.headers) if exc.headers else None,
    )

@app.exception_handler(404)
async def not_found_handler(request: Request, exc: Exception):
    req_id = getattr(request.state, "request_id", "??")
    return JSONResponse(
        status_code=404,
        content={"error": "Resource not found", "code": "NOT_FOUND", "path": str(request.url.path), "request_id": req_id},
    )

def _status_to_code(status: int) -> str:
    return {
        400: "BAD_REQUEST",
        401: "UNAUTHORIZED",
        403: "FORBIDDEN",
        404: "NOT_FOUND",
        422: "VALIDATION_ERROR",
        429: "RATE_LIMIT_EXCEEDED",
        500: "INTERNAL_SERVER_ERROR",
        503: "SERVICE_UNAVAILABLE",
    }.get(status, f"HTTP_{status}")

# 라우터 등록
app.include_router(ranked_router)    # /traders/ranked — traders보다 먼저 (경로 충돌 방지)
app.include_router(portfolio_router)
app.include_router(performance_router)  # /performance — 팔로워 실적 기록
app.include_router(traders_router)
app.include_router(builder_router)
app.include_router(followers_router)
app.include_router(pnl_router)       # /pnl/* — 팔로워 PnL 실적 조회
app.include_router(tracker_router)   # /tracker/* — 메인넷 장기 PnL 추적
app.include_router(presets_router)       # /presets/* — 시나리오 프리셋
app.include_router(papertrading_router)  # /papertrading/* — 4개 전략 페이퍼트레이딩 현황

# ── 인메모리 Rate Limiter ────────────────────────────
from collections import defaultdict
import time as _time_m

_rate_limit_store: dict = defaultdict(list)

# ── Rate Limit 정책 테이블 ───────────────────────────
# 엔드포인트 특성에 따라 차등 적용:
#   - 쓰기/온보딩: 엄격 (분당 10~20회)
#   - 읽기(조회): 관대 (분당 60~120회)
#   - 승인/서명:  중간 (분당 10회, 재시도 여유)
RATE_LIMIT_POLICY: dict[str, tuple[int, int]] = {
    # (max_calls, window_sec)
    # ── 쓰기 — 봇 방어, 재시도 여유 포함 ──────────────────────────────────
    "onboard":          (20,  60),   # 온보딩: 분당 20회 (재시도 포함)
    "follow":           (20,  60),   # 팔로우: 분당 20회
    "unfollow":         (20,  60),   # 언팔로우: 분당 20회
    "builder_approve":  (10,  60),   # Builder Code 승인: 분당 10회 (서명 재시도)
    # ── 읽기 — 30초 폴링 기준 (2req/min × 최대 10탭 = 20/min) ────────────
    "traders":          (120, 60),   # 트레이더 조회: 분당 120회 (충분)
    "markets":          (120, 60),   # 마켓 조회: 분당 120회 (30초 폴링 × 여러 탭)
    "trades":           (60,  60),   # 거래내역: 분당 60회 (120은 과도)
    "stats":            (60,  60),   # 통계 조회: 분당 60회
    "signals":          (30,  60),   # 시그널: 분당 30회 (펀딩/OI 계산 비용)
    "referral":         (30,  60),   # 레퍼럴: 분당 30회
    # ── 무거운 읽기 — CRS 계산 비용 ──────────────────────────────────────
    "ranked":           (30,  60),   # CRS 랭킹: 분당 30회 (코드-정책 통일, 충분)
    # ── 헬스/모니터링 — k8s probe + DDoS 방어 ────────────────────────────
    "health":           (180, 60),   # 헬스체크: 분당 180회 (k8s probe 허용, Rex 2026-03-18)
    "health_detailed":  (30,  60),   # 상세 헬스: 분당 30회 (내부 DB 데이터 포함, DDoS 방어)
    "default":          (60,  60),   # 기본: 분당 60회
}

def _get_ratelimit_status_safe() -> dict:
    """Pacifica rate limit 상태 — import 실패 시 빈 dict"""
    try:
        from pacifica.client import get_ratelimit_status
        return get_ratelimit_status()
    except Exception:
        return {}


def _check_rate_limit(key: str, max_calls: int = 10, window_sec: int = 60) -> bool:  # noqa: E501
    """True = 허용, False = 차단. Sliding window 방식."""
    now = _time_m.time()
    calls = _rate_limit_store[key]
    _rate_limit_store[key] = [t for t in calls if now - t < window_sec]
    if len(_rate_limit_store[key]) >= max_calls:
        return False
    _rate_limit_store[key].append(now)
    # 메모리 누수 방지: 키 수 1000 초과 시 정리
    if len(_rate_limit_store) > 1000:
        expired = [k for k, v in list(_rate_limit_store.items())
                   if not v or now - v[-1] > window_sec]
        for k in expired:
            del _rate_limit_store[k]
        # 여전히 1000 초과면 가장 오래된 키 강제 제거 (빈 리스트 안전 처리)
        if len(_rate_limit_store) > 1000:
            oldest = sorted(
                _rate_limit_store.items(),
                key=lambda x: x[1][-1] if x[1] else 0.0  # 빈 리스트 → 0 (가장 오래된 것으로 처리)
            )
            to_delete = oldest[:len(_rate_limit_store) - 1000]
            for k, _ in to_delete:
                _rate_limit_store.pop(k, None)  # 이미 삭제됐을 경우 KeyError 방지
    return True

def _is_in_trusted_range(host: str, trusted_proxy: str) -> bool:
    """CIDR 및 단일 IP 모두 지원하는 프록시 신뢰 검사"""
    import ipaddress
    try:
        host_ip = ipaddress.ip_address(host)
        for entry in trusted_proxy.split(","):
            entry = entry.strip()
            if not entry:
                continue
            try:
                if "/" in entry:
                    if host_ip in ipaddress.ip_network(entry, strict=False):
                        return True
                else:
                    if host_ip == ipaddress.ip_address(entry):
                        return True
            except ValueError:
                continue
    except ValueError:
        pass
    return False


def _get_client_ip(request: Request) -> str:
    """실제 클라이언트 IP 추출.
    X-Forwarded-For 스푸핑 방지: TRUSTED_PROXY_IPS 환경변수가 설정된 경우에만 헤더 신뢰.
    CIDR 표기법 지원 (예: 10.0.0.0/8 — Render 내부망).
    """
    trusted_proxy = os.getenv("TRUSTED_PROXY_IPS", "")
    client_host = request.client.host if request.client else "unknown"
    if trusted_proxy and client_host != "unknown":
        if _is_in_trusted_range(client_host, trusted_proxy):
            xff = request.headers.get("X-Forwarded-For", "")
            if xff:
                return xff.split(",")[0].strip()
    return client_host


def _require_rate_limit(key: str, max_calls: int = None, window_sec: int = 60, request: Request = None) -> None:
    """Rate limit 초과 시 HTTPException(429) 발생.
    max_calls 생략 시 RATE_LIMIT_POLICY 테이블에서 자동 조회.
    """
    # 정책 테이블 자동 조회
    endpoint = key.split(":")[0]  # "onboard:127.0.0.1" → "onboard"
    if max_calls is None:
        policy = RATE_LIMIT_POLICY.get(endpoint, RATE_LIMIT_POLICY["default"])
        max_calls, window_sec = policy

    if not _check_rate_limit(key, max_calls, window_sec):
        retry_after = window_sec  # 윈도우 종료까지 대기 권고
        raise HTTPException(
            status_code=429,
            headers={"Retry-After": str(retry_after)},
            detail={
                "error": "Rate limit exceeded. Please try again later.",
                "code": "RATE_LIMIT_EXCEEDED",
                "retry_after_seconds": retry_after,
                "limit": max_calls,
                "window_seconds": window_sec,
            }
        )


# ── Solana 주소 검증 유틸 ────────────────────────────
def _is_valid_solana_address(addr: str) -> bool:  # type-checked
    """base58 디코딩 + 32바이트 확인"""
    try:
        import base58 as _b58
        decoded = _b58.b58decode(addr)
        return len(decoded) == 32
    except Exception:
        return False

def _require_valid_solana_address(addr: str, field: str = "address") -> None:  # type-checked
    """주소 검증 실패 시 HTTPException(400) 발생"""
    if not addr or not isinstance(addr, str):
        raise HTTPException(
            status_code=400,
            detail={"error": f"{field} is required", "code": "INVALID_ADDRESS"}
        )
    if not _is_valid_solana_address(addr):
        raise HTTPException(
            status_code=400,
            detail={"error": f"Invalid Solana address: {field}", "code": "INVALID_ADDRESS"}
        )


# ── 전역 상태 ─────────────────────────────────────────
_db = None
_engine = None
_monitors: dict[str, PositionMonitor] = {}
_start_time: float = _time_module.time()
from core.data_collector import get_price_cache as _get_pc, is_connected as _dc_connected, start_polling as _dc_start
_fuul = FuulReferral()


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        _db = await init_db()
    return _db


# ── 가격 데이터 수집 — DataCollector REST 폴링 ──────────────
async def _sync_leaderboard_loop():
    """Pacifica 실제 리더보드 주기적 동기화 (DB 업서트)"""
    await asyncio.sleep(10)
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
                score = (pnl_30d/equity*0.6 + pnl_7d/equity*0.3 + (0.1 if pnl_1d > 0 else 0)) if equity > 0 else 0
                try:
                    from core.reliability import compute_crs
                    _crs_row = {
                        "address": addr, "pnl_all_time": pnl_all, "pnl_30d": pnl_30d,
                        "pnl_7d": pnl_7d, "pnl_1d": pnl_1d, "equity": equity,
                        "win_rate": float(t.get("win_rate", 0) or 0),
                        "oi_current": float(t.get("oi_current", 0) or 0),
                        "roi_30d": pnl_30d / equity if equity > 0 else 0,
                    }
                    _crs_result = compute_crs(_crs_row)
                    _tier = _crs_result.grade
                    _sharpe = _crs_result.crs / 10
                except Exception:
                    _tier = "C"
                    _sharpe = 0.0

                # UPSERT 패턴: INSERT OR IGNORE + UPDATE를 하나로 통일
                await _db.execute(
                    """INSERT INTO traders
                       (address, alias, total_pnl, followers,
                        pnl_1d, pnl_7d, pnl_30d, pnl_all_time, equity,
                        volume_7d, volume_30d, oi_current, active, tier, sharpe,
                        win_rate, roi_30d, last_synced)
                       VALUES (?,?,?,0, ?,?,?,?,?,?,?,?,1,?,?,
                               ?,?,strftime('%s','now'))
                       ON CONFLICT(address) DO UPDATE SET
                           alias         = COALESCE(excluded.alias, alias),
                           total_pnl     = excluded.total_pnl,
                           pnl_1d        = excluded.pnl_1d,
                           pnl_7d        = excluded.pnl_7d,
                           pnl_30d       = excluded.pnl_30d,
                           pnl_all_time  = excluded.pnl_all_time,
                           equity        = excluded.equity,
                           volume_7d     = excluded.volume_7d,
                           volume_30d    = excluded.volume_30d,
                           oi_current    = excluded.oi_current,
                           active        = 1,
                           tier          = excluded.tier,
                           sharpe        = COALESCE(NULLIF(sharpe, 0), excluded.sharpe),
                           win_rate      = CASE WHEN excluded.win_rate > 0 THEN excluded.win_rate ELSE win_rate END,
                           roi_30d       = excluded.roi_30d,
                           last_synced   = strftime('%s','now')""",
                    (
                        addr,
                        t.get("username") or addr[:8],
                        pnl_all,
                        pnl_1d, pnl_7d, pnl_30d, pnl_all, equity,
                        float(t.get("volume_7d", 0) or 0),
                        float(t.get("volume_30d", 0) or 0),
                        float(t.get("oi_current", 0) or 0),
                        _tier, _sharpe,
                        float(t.get("win_rate", 0) or 0),
                        float(equity and pnl_30d / equity * 100 if equity else 0),
                    )
                )
            await _db.commit()
        except Exception as e:
            logger.warning(f"[Leaderboard] 동기화 오류: {e}")
        await asyncio.sleep(60)


async def _winrate_refresh_loop():
    """win_rate 자동 갱신 — 1시간마다 Tier1 트레이더 trades/history 재수집
    raw 소켓 파싱 제거 → PacificaClient._cf_request 사용 (chunked 파싱 버그 방지)

    P0 Fix (Round 4):
    - 'close' side 필터를 pnl IS NOT NULL + cause != 'funding' 로 교체
      (Pacifica API side 필드값: 'open_long'/'close_long'/'open_short'/'close_short' 또는
       단순 'long'/'short', close trades는 pnl 필드가 채워져 있음)
    - 로깅 강화: API 응답 구조 + 매번 count 출력
    """
    await asyncio.sleep(60)
    while True:
        try:
            db = await get_db()
            async with db.execute(
                "SELECT address, alias FROM traders WHERE active=1 ORDER BY pnl_all_time DESC LIMIT 12"
            ) as cur:
                top_traders = await cur.fetchall()

            from pacifica.client import _cf_request, get_ratelimit_status
            import re as _re

            updated = 0
            for row in top_traders:
                addr = row[0]
                if not addr:
                    continue
                try:
                    # PacificaClient의 CF 요청 함수 재사용 (chunked 파싱 포함)
                    result = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda a=addr: _cf_request("GET", f"trades/history?account={a}&limit=200")
                    )
                    if not isinstance(result, dict):
                        logger.warning(f"[WinRate] {addr[:12]} API 응답 타입 이상: {type(result)}")
                        continue
                    trades = result.get('data', []) or []
                    if not trades:
                        logger.info(f"[WinRate] {addr[:12]} 거래 내역 없음 (data=[]) — 스킵")
                        continue

                    # P0 Fix: pnl 필드가 존재하는 거래 = 청산된(closed) 거래
                    # Pacifica API는 closing trade에만 pnl을 채움
                    # cause='funding' 은 펀딩비 정산이므로 제외
                    closes = [
                        t for t in trades
                        if t.get('pnl') is not None                              # pnl이 채워진 거래
                        and str(t.get('cause', '')).lower() != 'funding'         # 펀딩비 제외
                        and str(t.get('cause', '')).lower() != 'liquidation'     # 청산 제외 (win/loss 통계 왜곡)
                    ]
                    # fallback: pnl 없지만 side에 'close' 포함 (일부 API 버전 대응)
                    if not closes:
                        closes = [
                            t for t in trades
                            if 'close' in str(t.get('side', '')).lower()
                        ]

                    wins   = sum(1 for t in closes if float(t.get('pnl') or 0) > 0)
                    losses = sum(1 for t in closes if float(t.get('pnl') or 0) < 0)
                    total  = wins + losses
                    wr     = wins / total if total > 0 else 0.0

                    logger.info(
                        f"[WinRate] {addr[:12]} | raw={len(trades)} trades, "
                        f"closes={len(closes)}, W={wins} L={losses} → wr={wr:.2%}"
                    )

                    await db.execute(
                        "UPDATE traders SET win_rate=?, win_count=?, lose_count=? WHERE address=?",
                        (wr, wins, losses, addr)
                    )
                    updated += 1

                    # Rate limit 기반 동적 대기
                    rl = get_ratelimit_status()
                    if rl.get("credits_remaining", 999) < 100:
                        wait = max(rl.get("reset_in_seconds", 5), 5)
                        logger.warning(f"[WinRate] credit 부족({rl.get('credits_remaining')}) → {wait}초 대기")
                        await asyncio.sleep(wait)
                    else:
                        await asyncio.sleep(1.2)

                except Exception as e:
                    err_str = str(e)
                    if "429" in err_str:
                        m = _re.search(r'retry after (\d+)', err_str)
                        wait = int(m.group(1)) if m else 60
                        logger.warning(f"[WinRate] 429 → {wait}초 대기")
                        await asyncio.sleep(wait)
                    else:
                        logger.warning(f"[WinRate] 갱신 실패 {addr[:12]}: {e}")

            await db.commit()
            logger.info(f"[WinRate] 갱신 완료: {updated}/{len(top_traders)}명 업데이트")
        except Exception as e:
            logger.warning(f"[WinRate] 갱신 루프 오류: {e}")

        await asyncio.sleep(1 * 3600)  # 1시간마다 갱신 (프로덕트 기준)


@app.on_event("startup")
async def _startup_compat():
    """lifespan 미지원 환경 대비 deprecated fallback"""
    pass


async def _restore_monitors_from_db():
    """서버 재기동 후 DB의 active=1 팔로워가 팔로우하는 트레이더 monitor 자동 복원"""
    global _monitors, _engine
    await asyncio.sleep(2)
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


TOP_TRADERS = list({
    "EcX5xSDT45Nvhi2gMTjTnhF3KT2w4sPF54esEZS3hwZu",
    "4UBH19qUbXEaqyz9fKrFHuvj8BPMoM87H71s1YPKyGYq",
    "7C3sXQ6KvXJLkYGwzjNy2BHpkfEnRHzzfVAgUS64CDEd",
    "7gV81bz99MUBVb2aLYxW7MG1RMDdRdJYTPyC2syjba8y",
    "3rXoG6i55P7D1Q3tYsB7Unds8nBtKh7vH5VUyMDpWkSe",
    "5C9GKLrKFUvLWZEbMZQC5mtkTdKxuUhCzVCXZQH4FmCw",
    "EYhhf8u9M6kN9tCRVgd2Jki9fJm3XzJRnTF9k5eBC1q1",
    "A6VY4ZBUohgSLkwMuDwDvAnzgiXFB1eTDzaixyitPJep",
})

async def _auto_monitor_top_traders():
    """QA 추천 TOP 트레이더 자동 포지션 모니터링"""
    global _monitors, _engine
    await asyncio.sleep(3)
    for addr in TOP_TRADERS:
        if addr not in _monitors:
            try:
                monitor = RestPositionMonitor(addr, _engine.on_fill)
                _monitors[addr] = monitor
                asyncio.create_task(monitor.start())
                logger.info(f"[Auto] 모니터링 시작: {addr[:16]}...")
            except Exception as e:
                logger.warning(f"[Auto] 모니터 시작 실패 {addr[:12]}: {e}")
    logger.info(f"[Auto] TOP 트레이더 모니터링 완료: {len(_monitors)}개")


# ── 입력값 검증 유틸 ─────────────────────────────────
def _validate_copy_ratio(v: float) -> float:  # type-checked
    """copy_ratio: 0.01 ~ 1.0 범위 강제"""
    if v < 0.01 or v > 1.0:
        raise HTTPException(
            status_code=400,
            detail={"error": "copy_ratio must be between 0.01 and 1.0", "code": "INVALID_COPY_RATIO"}
        )
    return v

def _validate_max_position(v: float) -> float:  # type-checked
    """max_position_usdc: 1 ~ 10000 범위 강제"""
    if v < 1 or v > 10000:
        raise HTTPException(
            status_code=400,
            detail={"error": "max_position_usdc must be between 1 and 10000", "code": "INVALID_MAX_POSITION"}
        )
    return v


# ── 요청 모델 ─────────────────────────────────────────
BUILDER_CODE = os.getenv("BUILDER_CODE", "noivan")

class FollowRequest(BaseModel):
    follower_address: str
    trader_address: str
    copy_ratio: float = 0.5
    max_position_usdc: float = 50.0
    referrer_address: Optional[str] = None

    @field_validator("copy_ratio")
    @classmethod
    def validate_copy_ratio(cls, v):
        if v < 0.01 or v > 1.0:
            raise ValueError("copy_ratio must be between 0.01 and 1.0")
        return v

    @field_validator("max_position_usdc")
    @classmethod
    def validate_max_position_usdc(cls, v):
        if v < 1 or v > 10000:
            raise ValueError("max_position_usdc must be between 1 and 10000")
        return v

class UnfollowRequest(BaseModel):
    follower_address: str

class ReferralTrackRequest(BaseModel):
    referrer: str
    referee: str


# ── 기본 엔드포인트 ───────────────────────────────────
@app.get("/")
def root():
    from fastapi.responses import FileResponse
    import os
    frontend_index = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend", "index.html")
    if os.path.exists(frontend_index):
        return FileResponse(frontend_index)
    return {"status": "ok", "service": "Copy Perp", "version": APP_VERSION, "docs": "/docs"}


@app.get("/leaderboard")
async def leaderboard_alias(limit: int = 20) -> dict:
    """/traders의 alias — 프론트 호환성"""
    from db.database import get_leaderboard as _get_lb
    from pacifica.client import PacificaClient
    _pac = PacificaClient()
    try:
        real_lb = await asyncio.get_event_loop().run_in_executor(None, lambda: _pac.get_leaderboard(limit=limit))
        if isinstance(real_lb, list) and real_lb:
            return {"data": real_lb, "count": len(real_lb)}
    except Exception as e:
        logger.warning(f"[Leaderboard] Pacifica API 조회 실패: {e}")
    if _db:
        try:
            leaders = await _get_lb(_db, limit)
            return {"data": [dict(r) for r in leaders], "count": len(leaders)}
        except Exception as e:
            logger.error(f"[Leaderboard] DB 조회 실패: {e}")
            raise HTTPException(
                status_code=503,
                detail={"error": "Unable to load leaderboard", "code": "SERVICE_UNAVAILABLE"}
            )
    return {"data": [], "count": 0}


_STARTUP_AT = int(__import__("time").time())
try:
    import subprocess as _sp
    _GIT_REV = _sp.getoutput("git rev-parse --short HEAD 2>/dev/null").strip() or "unknown"
except Exception:
    _GIT_REV = "unknown"

@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "startup_at": _STARTUP_AT, "revision": _GIT_REV}

@app.get("/health")
async def health(request: Request) -> dict:
    client_ip = _get_client_ip(request)
    _require_rate_limit(f"health:{client_ip}")
    btc = _get_pc().get("BTC", {})
    monitors_detail = []
    for addr, mon in _monitors.items():
        last_poll = getattr(mon, "_last_poll_time", None)
        monitors_detail.append({
            "trader": addr,
            "last_poll_at": last_poll,
            "fail_count": getattr(mon, "_fail_count", 0),
        })

    db_path = os.getenv("DB_PATH", "copy_perp.db")
    try:
        db_size_bytes = os.path.getsize(db_path)
    except Exception:
        db_size_bytes = -1

    network_env = os.getenv("NETWORK", "testnet")
    mainnet_traders_count: Optional[int] = None
    testnet_traders_count: Optional[int] = None
    try:
        import sqlite3 as _sqlite3
        _db_path2 = os.getenv("DB_PATH", "copy_perp.db")

        def _count_active_traders():
            with _sqlite3.connect(_db_path2) as _sc:
                _row = _sc.execute("SELECT COUNT(*) FROM traders WHERE active=1").fetchone()
                return _row[0] if _row else 0

        active_cnt = await asyncio.get_event_loop().run_in_executor(None, _count_active_traders)
        if network_env == "mainnet":
            mainnet_traders_count = active_cnt
        else:
            testnet_traders_count = active_cnt
    except Exception as e:
        logger.debug(f"무시된 예외: {e}")

    return {
        "status": "ok",
        "network":        network_env,
        "data_connected": _dc_connected(),
        "ws_connected":   _dc_connected(),
        "data_source":    "rest_poll",
        "btc_mark":    btc.get("mark"),
        "btc_funding": btc.get("funding"),
        "btc_oi":      btc.get("open_interest"),
        "active_monitors": len(_monitors),
        "symbols_cached":  len(_get_pc()),
        "monitors_detail": monitors_detail,
        "uptime_seconds":  round(_time_module.time() - _start_time, 1),
        "db_size_bytes":   db_size_bytes,
        "mainnet_traders": mainnet_traders_count,
        "testnet_traders": testnet_traders_count,
        "privy_configured": bool(os.getenv("PRIVY_APP_ID", "")),
        "builder_fee_rate": os.getenv("BUILDER_FEE_RATE", "0.001"),
        "env_degraded": bool(os.getenv("_ENV_DEGRADED")),
        "version": APP_VERSION,
        "pacifica_ratelimit": _get_ratelimit_status_safe(),
    }


@app.get("/markets")
def get_markets(request: Request, symbol: Optional[str] = None) -> dict:
    client_ip = _get_client_ip(request)
    _require_rate_limit(f"markets:{client_ip}")
    if symbol:
        data = _get_pc().get(symbol.upper())
        if not data:
            raise HTTPException(
                status_code=404,
                detail={"error": f"Symbol {symbol} not found", "code": "NOT_FOUND"}
            )
        return {"data": data}
    items = sorted(_get_pc().values(), key=lambda x: abs(float(x.get("funding", 0))), reverse=True)
    return {"data": items, "count": len(items)}


@app.get("/signals")
def get_signals(request: Request, top_n: int = 5) -> dict:
    """실시간 시그널 — 펀딩비 극단 + Oracle-Mark 괴리"""
    client_ip = _get_client_ip(request)
    _require_rate_limit(f"signals:{client_ip}")
    # top_n 범위 방어 (음수 or 과도한 값)
    top_n = max(1, min(top_n, 50))
    items = list(_get_pc().values())
    funding_top = sorted(items, key=lambda x: abs(float(x.get("funding", 0))), reverse=True)[:top_n]
    raw_div = [m for m in items if float(m.get("oracle", 0)) > 0]
    for m in raw_div:
        oracle = float(m.get("oracle", 1))
        mark = float(m.get("mark", 0))
        m["divergence_pct"] = round((mark - oracle) / oracle * 100, 4) if oracle else 0.0
    divergence_top = sorted(raw_div, key=lambda x: abs(x.get("divergence_pct", 0)), reverse=True)[:top_n]
    return {
        "ok": True,
        "funding_extremes": funding_top,
        "oracle_mark_divergence": divergence_top,
        "source": "live" if _get_pc() else "empty",
    }


# ── 팔로우 ────────────────────────────────────────────
@app.post("/follow")
async def follow_trader(body: FollowRequest, background_tasks: BackgroundTasks, request: Request) -> dict:
    req_id = getattr(request.state, "request_id", "??")

    # Rate limit: IP당 분당 10회
    client_ip = _get_client_ip(request)
    _require_rate_limit(f"follow:{client_ip}", request=request)

    # Solana 주소 검증
    if not _is_valid_solana_address(body.follower_address):
        raise HTTPException(
            status_code=400,
            detail={"error": "Invalid follower_address (Solana address format)", "code": "INVALID_ADDRESS"}
        )
    if not _is_valid_solana_address(body.trader_address):
        raise HTTPException(
            status_code=400,
            detail={"error": "Invalid trader_address (Solana address format)", "code": "INVALID_ADDRESS"}
        )

    try:
        db = await get_db()
        await add_trader(db, body.trader_address)
        await add_follower(
            db,
            address=body.follower_address,
            trader_address=body.trader_address,
            copy_ratio=body.copy_ratio,
            max_position_usdc=body.max_position_usdc,
        )
    except Exception as e:
        logger.error(f"[{req_id}] follow DB 오류: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "Internal server error", "code": "INTERNAL_SERVER_ERROR"}
        )

    # 레퍼럴 추적
    if body.referrer_address:
        try:
            await _fuul.track_referral(body.referrer_address, body.follower_address)
        except Exception as e:
            logger.warning(f"[{req_id}] 레퍼럴 추적 실패: {e}")

    # 모니터 시작
    if body.trader_address not in _monitors:
        try:
            monitor = RestPositionMonitor(body.trader_address, _engine.on_fill)
            _monitors[body.trader_address] = monitor
            background_tasks.add_task(monitor.start)
        except Exception as e:
            logger.warning(f"[{req_id}] 모니터 시작 실패: {e}")

    return {
        "status": "ok",
        "follower": body.follower_address,
        "trader": body.trader_address,
        "copy_ratio": body.copy_ratio,
        "max_position_usdc": body.max_position_usdc,
        "builder_code": BUILDER_CODE,
        "monitoring": True,
        "note": f"Builder Code '{BUILDER_CODE}' — user signature approval required via frontend",
    }


@app.delete("/follow/{trader_address}")
async def unfollow_trader(trader_address: str, request: Request, follower_address: str = "", body: Optional[UnfollowRequest] = None) -> dict:
    req_id = getattr(request.state, "request_id", "??")

    # Rate limit: IP당 분당 10회
    client_ip = _get_client_ip(request)
    _require_rate_limit(f"unfollow:{client_ip}", request=request)

    # 주소 검증
    if not _is_valid_solana_address(trader_address):
        raise HTTPException(
            status_code=400,
            detail={"error": "Invalid trader_address", "code": "INVALID_ADDRESS"}
        )

    # query param 또는 body 중 follower_address 우선순위: query > body
    _follower_addr = follower_address or (body.follower_address if body else "")
    if not _follower_addr:
        raise HTTPException(status_code=422, detail={"error": "follower_address required", "code": "MISSING_PARAM"})

    try:
        db = await get_db()
        await db.execute(
            "UPDATE followers SET active=0 WHERE address=? AND trader_address=?",
            (_follower_addr, trader_address)
        )
        await db.commit()
    except Exception as e:
        logger.error(f"[{req_id}] unfollow DB 오류: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "Internal server error", "code": "INTERNAL_SERVER_ERROR"}
        )

    try:
        followers = await get_followers(db, trader_address)
        if not followers and trader_address in _monitors:
            await _monitors[trader_address].stop()
            del _monitors[trader_address]
    except Exception as e:
        logger.warning(f"[{req_id}] 모니터 중지 실패 (무시): {e}")

    return {"status": "ok", "unfollowed": trader_address}


# ── 거래 내역 ─────────────────────────────────────────
@app.get("/trades")
async def list_trades(
    request: Request,
    limit: int = 50,
    follower: Optional[str] = None,
    trader:   Optional[str] = None,
    status:   Optional[str] = None,
) -> dict:
    """Copy Trade 내역 조회 (필터: follower, trader, status)"""
    req_id = getattr(request.state, "request_id", "??")

    # Rate limit: IP당 분당 60회
    client_ip = _get_client_ip(request)
    _require_rate_limit(f"trades:{client_ip}", request=request)

    # 입력 검증
    if limit < 1 or limit > 500:
        raise HTTPException(
            status_code=400,
            detail={"error": "limit must be between 1 and 500", "code": "INVALID_LIMIT"}
        )
    if status and status not in ("filled", "pending", "failed"):
        raise HTTPException(
            status_code=400,
            detail={"error": "status must be one of: filled, pending, failed", "code": "INVALID_STATUS"}
        )

    try:
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
    except Exception as e:
        logger.error(f"[{req_id}] trades DB 오류: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "Unable to load trade history", "code": "INTERNAL_SERVER_ERROR"}
        )

    data = [dict(r) for r in rows]
    filled  = [r for r in data if r.get("status") == "filled"]
    failed  = [r for r in data if r.get("status") == "failed"]
    total_vol = sum(float(r.get("amount", 0) or 0) * float(r.get("price", 0) or 0) for r in filled)
    total_pnl = sum(float(r.get("pnl", 0) or 0) for r in filled)

    # 전체 DB 기준 집계 (필터 없는 경우 전체, 있으면 해당 필터 기준)
    try:
        _conds, _params = [], []
        if follower:
            _conds.append("follower_address=?"); _params.append(follower)
        if trader:
            _conds.append("trader_address=?");   _params.append(trader)
        _where_all = f"WHERE {' AND '.join(_conds)}" if _conds else ""
        async with db.execute(
            f"SELECT COUNT(*) FROM copy_trades {_where_all}", _params
        ) as _cur:
            _total_all = int((await _cur.fetchone())[0])
        # WHERE 또는 AND 분기: 조건이 이미 있으면 AND, 없으면 WHERE
        _join_kw = "AND" if _conds else "WHERE"
        async with db.execute(
            f"SELECT COUNT(*) FROM copy_trades {_where_all} {_join_kw} status='filled'",
            _params,
        ) as _cur:
            _filled_all = int((await _cur.fetchone())[0])
        async with db.execute(
            f"SELECT COUNT(*) FROM copy_trades {_where_all} {_join_kw} status='failed'",
            _params,
        ) as _cur:
            _failed_all = int((await _cur.fetchone())[0])
        async with db.execute(
            f"""SELECT COALESCE(SUM(CAST(amount AS REAL)*CAST(price AS REAL)),0)
                FROM copy_trades {_where_all} {_join_kw} status='filled'""",
            _params,
        ) as _cur:
            _vol_all = float((await _cur.fetchone())[0])
        async with db.execute(
            f"""SELECT COALESCE(SUM(pnl),0), COUNT(CASE WHEN pnl>0 THEN 1 END)
                FROM copy_trades {_where_all} {_join_kw} pnl IS NOT NULL""",
            _params,
        ) as _cur:
            _pnl_row = await _cur.fetchone()
            _pnl_all = float(_pnl_row[0])
            _win_all = int(_pnl_row[1])
        async with db.execute(
            f"""SELECT COUNT(*) FROM copy_trades {_where_all} {_join_kw} pnl<0""",
            _params,
        ) as _cur:
            _lose_all = int((await _cur.fetchone())[0])
        _wr = round(_win_all / max(_win_all + _lose_all, 1) * 100, 2) if (_win_all + _lose_all) > 0 else 0.0
    except Exception:
        _total_all = len(data)
        _filled_all = len(filled)
        _failed_all = len(failed)
        _vol_all = total_vol
        _pnl_all = total_pnl
        _win_all = 0
        _wr = 0.0

    return {
        "data": data,
        "count": len(data),
        "summary": {
            "total": _total_all,
            "filled": _filled_all,
            "failed": _failed_all,
            "realized_pnl_usdc": round(_pnl_all, 4),
            "total_pnl_usdc": round(_pnl_all, 4),
            "total_volume_usdc": round(_vol_all, 2),
            "win_rate_pct": _wr,
        },
    }


# ── 통계 ──────────────────────────────────────────────
@app.get("/stats/overview")
@app.get("/stats")
async def get_stats(request: Request) -> dict:
    req_id = getattr(request.state, "request_id", "??")

    # Rate limit: IP당 분당 60회
    client_ip = _get_client_ip(request)
    _require_rate_limit(f"stats:{client_ip}", request=request)

    # 30초 캐시 사용
    import time as _t
    _now = _t.time()
    if _STATS_CACHE["data"] and (_now - _STATS_CACHE["ts"]) < _STATS_CACHE_TTL:
        cached = dict(_STATS_CACHE["data"])
        cached["cached"] = True
        cached["cache_age_sec"] = round(_now - _STATS_CACHE["ts"], 1)
        return cached

    try:
        db = await get_db()
        stats = await get_platform_stats(db)
    except Exception as e:
        logger.warning(f"[{req_id}] stats 조회 오류: {e}")
        # fallback: 직접 DB 조회
        try:
            db = await get_db()
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
        except Exception as e2:
            logger.error(f"[{req_id}] stats fallback 오류: {e2}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail={"error": "Unable to load statistics", "code": "INTERNAL_SERVER_ERROR"}
            )

    stats["ws_symbols"] = len(_get_pc())
    stats["active_monitors"] = len(_monitors)
    try:
        db2 = await get_db()
        async with db2.execute("SELECT COALESCE(SUM(fee_usdc),0), COUNT(*) FROM fee_records") as cur:
            fee_sum, fee_count = await cur.fetchone()
        stats["builder_fee_total_usdc"] = round(float(fee_sum), 4)
        stats["builder_fee_count"] = fee_count
    except Exception:
        stats["builder_fee_total_usdc"] = 0.0
        stats["builder_fee_count"] = 0
    # 응답 표준화
    # 편의 alias 추가 (하위호환)
    stats["total_traders"] = stats.get("active_traders", 0)
    stats["total_followers"] = stats.get("active_followers", 0)
    stats["signals_count"] = len(_get_pc())  # 시그널 가능한 심볼 수
    stats["ok"] = True
    stats["version"] = APP_VERSION
    stats["network"] = os.getenv("NETWORK", "testnet")
    stats["cached"] = False
    _STATS_CACHE["ts"] = _t.time()
    _STATS_CACHE["data"] = dict(stats)
    return stats


# ── 메트릭 / 이벤트 로그 ──────────────────────────────
@app.get("/metrics")
async def get_metrics():
    """Prometheus 텍스트 형식 메트릭
    P2 Fix (Round 4): 실제 DB 집계 추가
    - copy_trades 건수 (today/total/failed)
    - follower 수
    - 오늘 빌더피 합계
    - 최근 24h PnL
    """
    from fastapi.responses import PlainTextResponse
    try:
        db = await get_db()
        s = await get_platform_stats(db)
    except Exception:
        s = {}
    btc = _get_pc().get("BTC", {})

    # ── DB 실제 집계 (P2 추가) ──────────────────────────────
    _today_trades = 0
    _today_fee = 0.0
    _today_pnl = 0.0
    _failed_trades = 0
    _total_followers_ever = 0
    try:
        db2 = await get_db()
        _today_start = int((_time_module.time() // 86400) * 86400 * 1000)  # UTC 자정 ms
        async with db2.execute(
            "SELECT COUNT(*) FROM copy_trades WHERE status='filled' AND created_at >= ?",
            (_today_start,)
        ) as _c:
            _today_trades = int((await _c.fetchone())[0])
        async with db2.execute(
            "SELECT COUNT(*) FROM copy_trades WHERE status='failed' AND created_at >= ?",
            (_today_start,)
        ) as _c:
            _failed_trades = int((await _c.fetchone())[0])
        async with db2.execute(
            "SELECT COALESCE(SUM(fee_usdc), 0) FROM fee_records WHERE created_at >= ?",
            (_today_start // 1000,)  # fee_records는 초 단위
        ) as _c:
            _today_fee = float((await _c.fetchone())[0])
        async with db2.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM copy_trades WHERE pnl IS NOT NULL AND created_at >= ?",
            (_today_start,)
        ) as _c:
            _today_pnl = float((await _c.fetchone())[0])
        async with db2.execute("SELECT COUNT(DISTINCT address) FROM followers") as _c:
            _total_followers_ever = int((await _c.fetchone())[0])
    except Exception as _me:
        logger.debug(f"[metrics] DB 집계 오류 (무시): {_me}")

    lines = [
        f"# HELP copy_perp_active_traders Number of active traders being monitored",
        f"# TYPE copy_perp_active_traders gauge",
        f"copy_perp_active_traders {s.get('active_traders', 0)}",
        f"# HELP copy_perp_active_followers Number of active followers",
        f"# TYPE copy_perp_active_followers gauge",
        f"copy_perp_active_followers {s.get('active_followers', 0)}",
        f"copy_perp_total_followers_ever {_total_followers_ever}",
        f"# HELP copy_perp_copy_trades_total Total copy trades (filled)",
        f"# TYPE copy_perp_copy_trades_total counter",
        f"copy_perp_copy_trades_total {s.get('total_trades_filled', 0)}",
        f"copy_perp_copy_trades_today {_today_trades}",
        f"copy_perp_copy_trades_failed_today {_failed_trades}",
        f"# HELP copy_perp_volume_usdc Total copy trade volume (USDC)",
        f"# TYPE copy_perp_volume_usdc counter",
        f"copy_perp_volume_usdc {s.get('total_volume_usdc', 0)}",
        f"# HELP copy_perp_builder_fee_today_usdc Builder fee collected today (USDC)",
        f"# TYPE copy_perp_builder_fee_today_usdc gauge",
        f"copy_perp_builder_fee_today_usdc {round(_today_fee, 6)}",
        f"copy_perp_builder_fee_total_usdc {s.get('builder_fee_total_usdc', 0)}",
        f"# HELP copy_perp_realized_pnl_today_usdc Realized PnL from copy trades today (USDC)",
        f"# TYPE copy_perp_realized_pnl_today_usdc gauge",
        f"copy_perp_realized_pnl_today_usdc {round(_today_pnl, 4)}",
        f"# HELP copy_perp_monitors_active Active position monitors",
        f"# TYPE copy_perp_monitors_active gauge",
        f"copy_perp_monitors_active {len(_monitors)}",
        f"copy_perp_btc_price {float(btc.get('mark', 0))}",
        f"copy_perp_btc_funding {float(btc.get('funding', 0))}",
        f"copy_perp_symbols_cached {len(_get_pc())}",
        f"copy_perp_uptime_seconds {round(_time_module.time() - _start_time, 1)}",
        f'copy_perp_network{{network="{os.getenv("NETWORK","testnet")}"}} 1',
    ]
    am = get_alert_manager()
    summary = am.get_error_summary()
    for k, v in summary.get("total_error_counts", {}).items():
        lines.append(f'copy_perp_order_errors{{follower="{k}"}} {v}')
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain")


@app.get("/events")
def get_events(limit: int = 50, level: Optional[str] = None) -> dict:
    """최근 시스템 이벤트 로그"""
    am = get_alert_manager()
    events = am.get_recent_events(limit=limit, level=level)
    return {
        "ok": True,
        "data": events,
        "count": len(events),
        "summary": am.get_error_summary(),
    }


@app.get("/stream")
async def sse_stream(request: Request):
    """SSE(Server-Sent Events) 실시간 스트림"""
    from fastapi.responses import StreamingResponse
    import json as _json

    async def event_generator():
        while True:
            # 클라이언트 연결 끊김 감지 — 연결 해제 시 루프 종료
            if await request.is_disconnected():
                logger.debug("SSE 클라이언트 연결 끊김 — 스트림 종료")
                break
            try:
                btc = _get_pc().get("BTC", {})
                db = await get_db()
                async with db.execute("SELECT COUNT(*) FROM traders WHERE active=1") as cur:
                    t_count = (await cur.fetchone())[0]
                async with db.execute("SELECT COUNT(*) FROM followers WHERE active=1") as cur:
                    f_count = (await cur.fetchone())[0]
                async with db.execute("SELECT COUNT(*) FROM copy_trades WHERE status='filled'") as cur:
                    trade_count = (await cur.fetchone())[0]

                data = _json.dumps({
                    "btc_mark":         btc.get("mark", "0"),
                    "btc_funding":      btc.get("funding", "0"),
                    "active_traders":   t_count,
                    "active_followers": f_count,
                    "trades_filled":    trade_count,
                    "monitors":         len(_monitors),
                    "ts":               int(_time_module.time()),
                })
                yield f"data: {data}\n\n"
            except Exception as e:
                logger.debug(f"SSE stream 오류: {e}")
                yield "data: {}\n\n"
            await asyncio.sleep(5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


# ── 레퍼럴 ────────────────────────────────────────────
@app.get("/fuul/leaderboard")
def referral_leaderboard(limit: int = 10) -> dict:
    data = _fuul.get_leaderboard(limit)
    return {"ok": True, "data": data, "count": len(data) if isinstance(data, list) else 0}


@app.post("/fuul/track")
async def track_referral(body: ReferralTrackRequest) -> dict:
    try:
        result = await _fuul.track_referral(body.referrer, body.referee)
        return result
    except Exception as e:
        logger.error(f"레퍼럴 추적 오류: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "Failed to track referral", "code": "INTERNAL_SERVER_ERROR"}
        )


@app.get("/referral/{address}")
def get_referral(address: str) -> dict:
    # Solana 주소 검증 — 유효하지 않으면 400 반환
    if not _is_valid_solana_address(address):
        raise HTTPException(
            status_code=400,
            detail={"error": "Invalid Solana address", "code": "INVALID_ADDRESS"}
        )
    try:
        link = _fuul.generate_referral_link(address)
        points = _fuul.get_points(address)
        return {"address": address, "referral_link": link, "points": points}
    except Exception as e:
        logger.error(f"레퍼럴 조회 오류: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "Unable to load referral info", "code": "INTERNAL_SERVER_ERROR"}
        )


@app.get("/health/detailed")
async def health_detailed(request: Request) -> dict:
    """상세 헬스 체크 (rate limit 적용 — DDoS 방어)"""
    # Rate limit: IP당 분당 60회 (health 정책 준용)
    client_ip = _get_client_ip(request)
    _require_rate_limit(f"health_detailed:{client_ip}")

    import core.data_collector as _dc_mod
    from core.data_collector import get_price_cache

    try:
        db = await get_db()
        async with db.execute("SELECT COUNT(*) FROM traders WHERE active=1") as c:
            trader_count = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(DISTINCT address) FROM followers WHERE active=1") as c:
            follower_count = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM copy_trades WHERE status='filled'") as c:
            filled_count = (await c.fetchone())[0]
        async with db.execute("SELECT COALESCE(SUM(CASE WHEN pnl IS NOT NULL THEN pnl ELSE 0 END),0) FROM copy_trades WHERE status='filled'") as c:
            total_pnl = (await c.fetchone())[0]
        db_ok = True
    except Exception as e:
        logger.error(f"health/detailed DB error: {e}")
        db_ok = False
        trader_count = follower_count = filled_count = 0
        total_pnl = 0

    now = _time_module.time()
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

    uptime_sec = round(now - _start_time, 1)
    db_path = os.getenv("DB_PATH", "copy_perp.db")
    try:
        db_size = os.path.getsize(db_path)
    except Exception:
        db_size = None

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


# ── 클라이언트 설정 제공 ──────────────────────────────
@app.get("/config")
def get_config() -> dict:
    privy_app_id = os.getenv("PRIVY_APP_ID", "")
    fuul_key = os.getenv("FUUL_API_KEY", "")
    return {
        "privy_app_id":      privy_app_id,
        "privy_configured":  bool(privy_app_id),
        "builder_code":      BUILDER_CODE,
        "builder_fee_rate":  os.getenv("BUILDER_FEE_RATE", "0.001"),
        "network":           os.getenv("NETWORK", "testnet"),
        "mock_mode":         not bool(os.getenv("AGENT_PRIVATE_KEY", "")),
        "fuul_enabled":      bool(fuul_key),
        # allowed_origins 미노출 (보안): 내부 CORS 정책은 서버에서만 관리
    }


# ── Builder Code 자동 승인 헬퍼 ───────────────────────
async def _auto_approve_builder(address: str):
    """팔로워 온보딩 시 Builder Code 자동 승인 (백그라운드)"""
    try:
        from pacifica.builder_code import approve
        from solders.keypair import Keypair
        import base58 as _b58
        pk = os.getenv("AGENT_PRIVATE_KEY", "")
        if not pk:
            return
        kp     = Keypair.from_seed(_b58.b58decode(pk)[:32])
        result = approve(account=address, keypair=kp)
        logger.info(f"[Builder] 자동 승인: {address[:16]}... → ok={result.get('ok')}")
    except Exception as e:
        logger.debug(f"[Builder] 자동 승인 실패 (무시): {e}")



# ── 프론트엔드 정적 파일 (마지막에 마운트) ────────────
frontend_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
if os.path.exists(frontend_path):
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")
