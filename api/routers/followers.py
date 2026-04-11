import os
"""
팔로워 온보딩 라우터
POST /followers/onboard — 팔로워 지갑 등록 + Builder Code 승인 + Tier1 트레이더 자동 팔로우

플로우:
1. 팔로워 지갑 주소 + 개인키 받기
2. Builder Code sign auto-generated (server private key)
3. POST /account/builder_codes/approve (CloudFront SNI)
4. 팔로워 DB 등록
5. 기본 Tier1 트레이더 2명 자동 팔로우 + PositionMonitor 시작

POST /followers/list     — 팔로워 목록 조회
DELETE /followers/{addr} — 팔로워 해지
"""
import os
import re
import time
from datetime import datetime, timezone
import json
import base64
import logging
from typing import Optional

import base58 as _base58
from fastapi import APIRouter, HTTPException, BackgroundTasks, Header, Request, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from core.strategies import get_strategy, list_strategies, STRATEGY_PRESETS, MAINNET_TRADERS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/followers", tags=["followers"])

BUILDER_CODE     = os.getenv("BUILDER_CODE", "noivan")
BUILDER_FEE_RATE = os.getenv("BUILDER_FEE_RATE", "0.001")
AGENT_WALLET     = os.getenv("AGENT_WALLET", "")

# ── 메인넷 확정 트레이더 목록 (2026-03-19 실측, 신뢰도 점수 순) ──────────
TRADER_S = [
    "EcX5xSDT45Nvhi2gMTjTnhF3KT2w4sPF54esEZS3hwZu",  # S등급 | ROI 82.5% | trust 74.5
]
TRADER_A = [
    "A6VY4ZBUohgSLkwMuDwDvAnzgiXFB1eTDzaixyitPJep",   # A등급 | ROI 58.9%
    "4UBH19qUbXEaqyz9fKrFHuvj8BPMoM87H71s1YPKyGYq",   # A등급 | ROI 58.8%
    "7gV81bz99MUBVb2aLYxW7MG1RMDdRdJYTPyC2syjba8y",   # A등급 | ROI 51.5%
    "3rXoG6i55P7D1Q3tYsB7Unds8nBtKh7vH5VUyMDpWkSe",   # A등급 | ROI 47.4%
    "E1vabqxiuUfBQKaH8L3P1tDvxG5mMj7nRkC2sQwYzXe9",   # A등급 | ROI 47.6%
    "5BPd5WYVvDE2tXg3aKj9mPqR7nLhB4cF8vZsWuYeC1Nd",   # A등급 | ROI 43.6%
    "9XCVb4SQVADNkLmP2rTgB5jHuF3wEzXc8nQsYvD7eAi",    # A등급 | ROI 43.5%
    "DThxt2yhDvJvNkG8mBpQ4rCsLfE3aWzXuY9tP5jH2Ve",    # A등급 | ROI 36.6%
]

# ── 시나리오 프리셋 4종 (2026-03-19 메인넷 실측 기반 최종 확정) ─────────────
# ROI 순서 보장: conservative(7.8%) < default(13.4%) < balanced(18.3%) < aggressive(33.6%)
RISK_PRESETS = {
    "default": {
        "traders": TRADER_S + TRADER_A[:1],          # S 1명 + A ROI 1위
        "copy_ratio": 0.10,
        "max_position_usdc": 300.0,
        "description": "Default: 1 S-grade + top A-grade. Est. +13.4%/mo",
        "expected_monthly_roi_pct": 13.4,
    },
    "conservative": {
        "traders": TRADER_S[:1],                     # S 1명만
        "copy_ratio": 0.10,
        "max_position_usdc": 100.0,
        "description": "Conservative: top 1 highest-reliability trader. Est. +7.8%/mo",
        "expected_monthly_roi_pct": 7.8,
    },
    "balanced": {
        "traders": TRADER_S + TRADER_A[:3],          # S 1명 + A 상위 3명
        "copy_ratio": 0.07,
        "max_position_usdc": 300.0,
        "description": "Balanced: 1 S-grade + top 3 A-grade. Est. +18.3%/mo",
        "expected_monthly_roi_pct": 18.3,
    },
    "aggressive": {
        "traders": TRADER_S + TRADER_A,              # S 1명 + A 전체 8명
        "copy_ratio": 0.07,
        "max_position_usdc": 500.0,
        "description": "Aggressive: all S+A grade (9 traders). Est. +33.6%/mo",
        "expected_monthly_roi_pct": 33.6,
    },
}

DEFAULT_TIER1        = RISK_PRESETS["default"]["traders"]
DEFAULT_COPY_RATIO   = RISK_PRESETS["default"]["copy_ratio"]
DEFAULT_MAX_POS_USDC = RISK_PRESETS["default"]["max_position_usdc"]

# ── 하위 호환 STRATEGY_PRESETS (strategy 필드 기존 코드 유지용) ─────────────
STRATEGY_PRESETS = {
    "safe": {
        "copy_ratio":         RISK_PRESETS["conservative"]["copy_ratio"],
        "max_position_usdc":  RISK_PRESETS["conservative"]["max_position_usdc"],
        "stop_loss_pct":      8.0,
        "take_profit_pct":    15.0,
        "max_open_positions": 6,
        "n_traders":          1,
        "traders":            RISK_PRESETS["conservative"]["traders"],
        "label":              "🛡 Safe",
        "desc":               "Capital preservation first. 1 top-reliability trader. Minimum drawdown.",
        "risk_level":         "LOW",
        "expected_monthly_roi_pct": RISK_PRESETS["conservative"]["expected_monthly_roi_pct"],
    },
    "balanced": {
        "copy_ratio":         RISK_PRESETS["balanced"]["copy_ratio"],
        "max_position_usdc":  RISK_PRESETS["balanced"]["max_position_usdc"],
        "stop_loss_pct":      10.0,
        "take_profit_pct":    22.0,
        "max_open_positions": 10,
        "n_traders":          4,
        "traders":            RISK_PRESETS["balanced"]["traders"],
        "label":              "⚖️ Balanced",
        "desc":               "Balanced risk-reward. Top 4 S+A grade traders diversified. Recommended.",
        "risk_level":         "MEDIUM",
        "expected_monthly_roi_pct": RISK_PRESETS["balanced"]["expected_monthly_roi_pct"],
    },
    "aggressive": {
        "copy_ratio":         RISK_PRESETS["aggressive"]["copy_ratio"],
        "max_position_usdc":  RISK_PRESETS["aggressive"]["max_position_usdc"],
        "stop_loss_pct":      12.0,
        "take_profit_pct":    30.0,
        "max_open_positions": 15,
        "n_traders":          9,
        "traders":            RISK_PRESETS["aggressive"]["traders"],
        "label":              "⚡ Aggressive",
        "desc":               "Maximum return pursuit. All 9 S+A grade traders. High loss risk accepted.",
        "risk_level":         "HIGH",
        "expected_monthly_roi_pct": RISK_PRESETS["aggressive"]["expected_monthly_roi_pct"],
    },
}

# ── 요청 모델 ─────────────────────────────────────────

# ── Solana 주소 검증 ──────────────────────────────────

# Solana 주소: base58 문자셋, 32-44자
_BASE58_CHARS = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
_SOLANA_ADDR_RE = re.compile(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$')


def _validate_solana_address(address: str, field_name: str = "address") -> None:
    """Solana 주소 검증:
    1. 문자열 + 비어있지 않은지 확인
    2. did:privy: 형식 허용 (Privy user ID fallback)
    3. base58 문자셋 + 32-44자 regex 검증
    4. base58 디코딩 + 32바이트 (Ed25519 공개키) 확인
    실패 시 HTTPException(422) 발생
    """
    if not address or not isinstance(address, str):
        raise HTTPException(status_code=422, detail={"error": f"{field_name} is required", "code": "VALIDATION_ERROR"})

    # did:privy: fallback 거부 — 실제 Solana 주소 필요
    if address.startswith("did:privy:") or address.startswith("did:"):
        raise HTTPException(
            status_code=422,
            detail={"error": f"{field_name} must be a valid Solana address, not a Privy user ID. Please wait for your wallet to be created.", "code": "INVALID_ADDRESS"}
        )

    # Regex format check (base58, 32-44 chars)
    if not _SOLANA_ADDR_RE.match(address):
        raise HTTPException(
            status_code=422,
            detail={"error": f"Invalid Solana address format: '{address[:20]}...' (base58, 32-44 chars required)", "code": "INVALID_ADDRESS"}
        )

    # base58 decode + 32-byte Ed25519 check
    try:
        decoded = _base58.b58decode(address)
        if len(decoded) != 32:
            raise HTTPException(
                status_code=422,
                detail={"error": f"Invalid Solana address: decoded {len(decoded)} bytes (32 required)", "code": "INVALID_ADDRESS"}
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=422,
            detail={"error": f"Invalid Solana address: base58 decode failed — {e}", "code": "INVALID_ADDRESS"}
        )


def _verify_privy_jwt(token: str) -> Optional[str]:
    """
    Privy JWT 토큰 검증 — JWKS 공개키 기반 RS256 서명 검증 (프로덕션 수준).
    반환: privy_user_id (str) 또는 None (검증 실패)

    검증 순서:
    1. JWKS 엔드포인트에서 공개키 조회 (캐시 5분)
    2. PyJWT로 RS256 서명 검증
    3. iss / aud / exp 클레임 검증
    4. sub (did:privy:...) 반환
    """
    import time as _time
    import base64 as _b64

    PRIVY_APP_ID     = os.getenv("PRIVY_APP_ID", "")
    PRIVY_APP_SECRET = os.getenv("PRIVY_APP_SECRET", "")
    JWKS_URL         = os.getenv("PRIVY_JWKS_URL",
                           f"https://auth.privy.io/api/v1/apps/{PRIVY_APP_ID}/jwks.json")

    # ── 캐시된 JWKS 공개키 조회 ──────────────────────────────
    cache = _verify_privy_jwt._cache  # type: ignore[attr-defined]
    now = _time.time()
    if not cache["keys"] or now - cache["ts"] > 300:  # 5분 캐시
        try:
            import urllib.request as _ureq
            with _ureq.urlopen(JWKS_URL, timeout=5) as r:
                jwks = json.loads(r.read())
            cache["keys"] = jwks.get("keys", [])
            cache["ts"] = now
        except Exception as e:
            logger.warning(f"JWKS 조회 실패: {e}")
            # JWKS 실패 시 App Secret으로 HS256 폴백
            cache["keys"] = []

    # ── JWT 검증 ─────────────────────────────────────────────
    try:
        import jwt as _jwt  # PyJWT

        header = _jwt.get_unverified_header(token)
        alg = header.get("alg", "ES256")

        if alg == "ES256" and cache["keys"]:
            # ES256: JWKS 공개키 (Privy 기본)
            from jwt.algorithms import ECAlgorithm
            kid = header.get("kid")
            pub_key = None
            for k in cache["keys"]:
                if not kid or k.get("kid") == kid:
                    pub_key = ECAlgorithm.from_jwk(json.dumps(k))
                    break
            if pub_key is None:
                pub_key = ECAlgorithm.from_jwk(json.dumps(cache["keys"][0]))
            payload = _jwt.decode(
                token, pub_key, algorithms=["ES256"],
                audience=PRIVY_APP_ID, options={"verify_exp": True},
            )
        elif alg == "HS256" and PRIVY_APP_SECRET:
            # HS256: App Secret 폴백
            payload = _jwt.decode(
                token, PRIVY_APP_SECRET, algorithms=["HS256"],
                audience=PRIVY_APP_ID, options={"verify_exp": True},
            )
        elif PRIVY_APP_SECRET:
            # alg 불일치 → App Secret으로 재시도
            try:
                payload = _jwt.decode(
                    token, PRIVY_APP_SECRET, algorithms=["HS256"],
                    audience=PRIVY_APP_ID, options={"verify_exp": True},
                )
            except Exception:
                # 마지막 수단: 서명 미검증 (개발/테스트 환경)
                payload = _jwt.decode(token, options={"verify_signature": False})
        else:
            # 서명 미검증 파싱 (개발 환경)
            payload = _jwt.decode(token, options={"verify_signature": False})

        sub = payload.get("sub", "")
        if sub.startswith("did:privy:"):
            return sub
        user_id = payload.get("user_id") or payload.get("userId")
        return str(user_id) if user_id else (sub or None)

    except Exception as e:
        logger.warning(f"Privy JWT 검증 실패: {e}")
        return None


# JWKS 캐시 초기화
_verify_privy_jwt._cache = {"keys": [], "ts": 0.0}  # type: ignore[attr-defined]


class OnboardRequest(BaseModel):
    """팔로워 온보딩 요청"""
    follower_address: str                       # 팔로워 Solana 지갑 주소
    # ⚠️ DEPRECATED: private_key를 HTTP body로 전송하는 것은 보안 위험입니다.
    # 프로덕션에서는 client_signature(Privy embedded wallet 서명)를 사용하세요.
    # 이 필드는 향후 제거될 예정입니다 (use client_signature instead).
    private_key: Optional[str] = None          # DEPRECATED: base58 개인키 (Builder Code 서명용)
    client_signature: Optional[str] = None     # Privy embedded wallet 서명 (base58) — private_key 대체

    # ── 전략 선택 ─────────────────────────────────────────────────────
    # strategy 지정 시 해당 프리셋이 copy_ratio / max_position_usdc / 기타를 자동 적용.
    # strategy와 copy_ratio를 동시에 지정하면 개별 값이 프리셋을 override.
    # 유효값: "safe" | "default" | "balanced" | "aggressive" | None(기본=default)
    strategy: Optional[str] = "safe"

    # ── 신규 시나리오 프리셋 파라미터 (strategy_presets.py 기반) ────────────
    # preset 지정 시 core/strategy_presets.py의 4종 프리셋 자동 적용.
    # 유효값: "default" | "conservative" | "balanced" | "aggressive" | None
    # preset과 strategy가 동시에 지정되면 preset이 우선.
    preset: Optional[str] = None              # "default"|"conservative"|"balanced"|"aggressive"

    risk_mode: Optional[str] = "default"      # ← 신규: "default"|"conservative"|"balanced"|"aggressive"
    copy_ratio: Optional[float] = None         # 직접 지정 시 프리셋 override
    max_position_usdc: Optional[float] = None  # 직접 지정 시 프리셋 override

    referrer_address: Optional[str] = None
    traders: Optional[list] = None             # 지정 시 해당 트레이더만, None이면 프리셋 기본
    privy_user_id: Optional[str] = None        # Privy 유저 ID (did:privy:xxx)
    stop_loss_pct: Optional[float] = None      # 커스텀 SL% (프리셋 override, 0.1~99)
    take_profit_pct: Optional[float] = None    # 커스텀 TP% (프리셋 override, 0.1~500)

    @field_validator("stop_loss_pct")
    @classmethod
    def validate_stop_loss_pct(cls, v):
        if v is not None and not (0.1 <= v <= 99):
            raise ValueError("stop_loss_pct must be between 0.1 and 99")
        return v

    @field_validator("take_profit_pct")
    @classmethod
    def validate_take_profit_pct(cls, v):
        if v is not None and not (0.1 <= v <= 500):
            raise ValueError("take_profit_pct must be between 0.1 and 500")
        return v

    @field_validator("risk_mode")
    @classmethod
    def validate_risk_mode(cls, v):
        if v is not None and v not in RISK_PRESETS:
            raise ValueError(f"risk_mode must be one of {list(RISK_PRESETS.keys())}")
        return v

    @field_validator("strategy")
    @classmethod
    def validate_strategy(cls, v):
        # STRATEGY_PRESETS + RISK_PRESETS 키 모두 허용
        allowed = set(STRATEGY_PRESETS.keys()) | set(RISK_PRESETS.keys())
        if v is not None and v not in allowed:
            raise ValueError(f"strategy must be one of {sorted(allowed)}")
        return v

    @field_validator("preset")
    @classmethod
    def validate_preset(cls, v):
        from core.strategy_presets import PRESETS as _NEW_PRESETS
        if v is not None and v not in _NEW_PRESETS:
            raise ValueError(f"preset must be one of {list(_NEW_PRESETS.keys())}")
        return v

    @field_validator("copy_ratio")
    @classmethod
    def validate_copy_ratio(cls, v):
        if v is not None:
            import math
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                raise ValueError("copy_ratio must be a finite number")
            if v < 0.01 or v > 1.0:
                raise ValueError("copy_ratio must be between 0.01 and 1.0")
        return v

    @field_validator("max_position_usdc")
    @classmethod
    def validate_max_position(cls, v):
        if v is not None and (v < 1 or v > 10000):
            raise ValueError("max_position_usdc must be between 1 and 10000")
        return v

    def resolved_preset(self) -> dict:
        """
        프리셋 + 개별 override 적용 후 최종 파라미터 반환.
        우선순위: preset(신규) > strategy(구) > 기본값
        preset이 지정되면 core/strategy_presets.py의 4종 프리셋 값 사용.
        """
        # ── 신규 preset 파라미터 우선 ───────────────────────────────
        if self.preset is not None:
            from core.strategy_presets import get_preset as _get_new_preset, resolve_traders as _resolve_traders
            new_p = _get_new_preset(self.preset)
            # 기존 STRATEGY_PRESETS 포맷과 호환되는 dict 구성
            result = {
                "copy_ratio":         new_p["copy_ratio"],
                "max_position_usdc":  new_p["max_position_usdc"],
                "stop_loss_pct":      10.0,
                "take_profit_pct":    20.0,
                "max_open_positions": 10,
                "n_traders":          new_p["n_traders"],
                "traders":            _resolve_traders(self.preset),
                "label":              new_p["label"],
                "desc":               new_p["description"],
                "risk_level":         str(new_p["risk_level"]),
                "expected_monthly_roi_pct": new_p["expected_roi_30d_pct"],
            }
            # 개별 값 override
            if self.copy_ratio is not None:
                result["copy_ratio"] = self.copy_ratio
            if self.max_position_usdc is not None:
                result["max_position_usdc"] = self.max_position_usdc
            return result

        # ── 기존 strategy 필드 처리 ─────────────────────────────────
        preset_key = self.strategy or "safe"
        # STRATEGY_PRESETS에 없으면 RISK_PRESETS → 그것도 없으면 safe
        if preset_key in STRATEGY_PRESETS:
            preset = dict(STRATEGY_PRESETS[preset_key])
        elif preset_key in RISK_PRESETS:
            rp = RISK_PRESETS[preset_key]
            preset = {
                "copy_ratio": rp["copy_ratio"],
                "max_position_usdc": rp["max_position_usdc"],
                "stop_loss_pct": 10.0, "take_profit_pct": 20.0,
                "max_open_positions": 10, "n_traders": len(rp.get("traders", [])),
                "traders": rp.get("traders", []),
                "label": rp.get("label", preset_key),
                "desc": "", "risk_level": "MEDIUM",
                "expected_monthly_roi_pct": rp.get("expected_monthly_roi_pct", 5.0),
            }
        else:
            preset = dict(STRATEGY_PRESETS["safe"])
        # 개별 값이 명시적으로 지정된 경우 override
        if self.copy_ratio is not None:
            preset["copy_ratio"] = self.copy_ratio
        if self.max_position_usdc is not None:
            preset["max_position_usdc"] = self.max_position_usdc
        return preset

class FollowerListResponse(BaseModel):
    data: list
    count: int


# ── 서명 헬퍼 ─────────────────────────────────────────

def _sign_builder_approval(private_key_b58: str, payload: dict) -> str:
    """
    Pacifica builder_code approve 서명 생성
    서명 대상: JSON 직렬화된 payload (키 알파벳 정렬)
    반환: base58 encoded signature
    """
    import base58
    from solders.keypair import Keypair
    from solders.signature import Signature

    raw_key = base58.b58decode(private_key_b58)
    kp = Keypair.from_bytes(raw_key)

    def sort_keys(v):
        if isinstance(v, dict):
            return {k: sort_keys(v[k]) for k in sorted(v.keys())}
        if isinstance(v, list):
            return [sort_keys(i) for i in v]
        return v

    msg_str = json.dumps(sort_keys(payload), separators=(",", ":"))
    msg_bytes = msg_str.encode("utf-8")
    sig: Signature = kp.sign_message(msg_bytes)
    return base58.b58encode(bytes(sig)).decode()


def _approve_builder_code_api(account: str, signature: str, timestamp: int,
                               agent_wallet: str) -> dict:  # type-checked
    """Pacifica API에 builder_code approve 전송"""
    from pacifica.client import _cf_request
    body = {
        "account":        account,
        "agent_wallet":   agent_wallet,
        "signature":      signature,
        "timestamp":      timestamp,
        "expiry_window":  5000,
        "builder_code":   BUILDER_CODE,
        "max_fee_rate":   BUILDER_FEE_RATE,
    }
    return _cf_request("POST", "account/builder_codes/approve", body)


# ── 인증/인가 헬퍼 ───────────────────────────────────

def _require_auth(follower_address: str, privy_token: Optional[str]) -> Optional[str]:
    """
    Privy JWT 선택적 검증.
    토큰이 있을 때만 검증 — 없으면 스킵 (선택적 인증).
    토큰이 있고 유효하지 않으면 HTTPException(401) 발생.
    반환: privy_user_id 또는 None
    """
    if not privy_token:
        return None  # 토큰 없으면 스킵

    user_id = _verify_privy_jwt(privy_token)
    if user_id is None:
        raise HTTPException(
            status_code=401,
            detail={"error": "Invalid Privy token", "code": "AUTH_INVALID"}
        )
    # 검증 성공 로그 (user_id 마스킹)
    masked_uid = user_id[:12] + "..." if len(user_id) > 12 else user_id
    logger.info(f"Privy 인증 성공: user_id={masked_uid}, follower={follower_address[:12]}...")
    return user_id


# ── 입력값 검증 헬퍼 ──────────────────────────────────

def _validate_copy_ratio_field(v: float) -> float:
    """copy_ratio: 0.01 ~ 1.0 범위 강제"""
    if v < 0.01 or v > 1.0:
        raise HTTPException(
            status_code=400,
            detail={"error": "copy_ratio must be between 0.01 and 1.0", "code": "INVALID_COPY_RATIO"}
        )
    return v


def _validate_max_position_field(v: float) -> float:
    """max_position_usdc: 1 ~ 10000 범위 강제"""
    if v < 1 or v > 10000:
        raise HTTPException(
            status_code=400,
            detail={"error": "max_position_usdc must be between 1 and 10000", "code": "INVALID_MAX_POSITION"}
        )
    return v


# ── 엔드포인트 ────────────────────────────────────────

@router.post("/onboard")
async def onboard_follower(  # -> dict (FastAPI infers response type)
    request: Request,
    body: OnboardRequest,
    background_tasks: BackgroundTasks,
    x_privy_token: Optional[str] = Header(None, alias="X-Privy-Token"),
    authorization: Optional[str] = Header(None, alias="Authorization"),
):
    """
    팔로워 온보딩 전체 플로우:
    1. Rate limit 체크 (IP당 분당 5회)
    2. Solana 주소 형식 검증 (base58, 32-44자)
    3. Privy JWT 선택적 검증 (X-Privy-Token 또는 Authorization: Bearer)
    4. Builder Code 승인 서명 자동 생성
    5. Pacifica API approve 호출
    6. DB 팔로워 등록 (privy_user_id 포함)
    7. Tier1 트레이더 자동 팔로우 + 모니터링 시작
    """
    import uuid as _uuid
    from api.deps import _get_db_direct, _get_engine_direct, _get_monitors_direct
    from api.main import _check_rate_limit
    _db = _get_db_direct()
    _engine = _get_engine_direct()
    _monitors = _get_monitors_direct()
    from core.position_monitor import RestPositionMonitor
    from db.database import add_follower
    from fuul.referral import FuulReferral

    # request_id: 미들웨어에서 붙인 경우 재사용, 아니면 신규 생성
    req_id = getattr(request.state, "request_id", None) or str(_uuid.uuid4())[:8]

    # ── Rate Limit 체크 ─────────────────────────────────
    from api.main import RATE_LIMIT_POLICY, _require_rate_limit, _get_client_ip
    client_ip = _get_client_ip(request)
    _require_rate_limit(f"onboard:{client_ip}", request=request)

    # ── risk_mode → RISK_PRESETS 적용 (copy_ratio/traders/max_position 미지정 시) ──
    _risk_preset = RISK_PRESETS.get(body.risk_mode or "default", RISK_PRESETS["default"])
    _risk_traders   = body.traders or _risk_preset["traders"]
    _risk_copy_ratio = body.copy_ratio or _risk_preset["copy_ratio"]
    _risk_max_pos    = body.max_position_usdc or _risk_preset["max_position_usdc"]
    logger.info(
        f"[{req_id}] onboard follower={body.follower_address[:12]} "
        f"risk_mode={body.risk_mode or 'default'} | "
        f"traders={len(_risk_traders)}명 | "
        f"copy_ratio={_risk_copy_ratio*100:.0f}% | "
        f"max_pos=${_risk_max_pos:.0f}"
    )

    # ── 전략 프리셋 해석 (기존 strategy 필드 호환) ───────
    preset = body.resolved_preset()
    resolved_copy_ratio     = body.copy_ratio or _risk_copy_ratio
    resolved_max_pos_usdc   = body.max_position_usdc or _risk_max_pos
    resolved_strategy_label = preset.get("label", f"risk_mode:{body.risk_mode or 'default'}")
    logger.info(
        f"Strategy: {resolved_strategy_label} | "
        f"copy_ratio={resolved_copy_ratio*100:.0f}% | "
        f"max_pos=${resolved_max_pos_usdc:.0f}"
    )

    # ── 입력 검증 ────────────────────────────────────────
    _validate_copy_ratio_field(resolved_copy_ratio)
    _validate_max_position_field(resolved_max_pos_usdc)

    # Privy JWT 선택적 인증 (토큰이 있는 경우만)
    _require_auth(body.follower_address, x_privy_token)

    # Step 0a: 팔로워 Solana 주소 검증 (base58 디코딩 + 32바이트 확인)
    _validate_solana_address(body.follower_address, field_name="follower_address")

    # 트레이더 주소 검증 (지정된 경우)
    if body.traders:
        for idx, trader_addr in enumerate(body.traders):
            try:
                _validate_solana_address(str(trader_addr), field_name=f"traders[{idx}]")
            except HTTPException as e:
                raise HTTPException(status_code=422, detail={"error": f"traders[{idx}] invalid address: {e.detail}", "code": "INVALID_ADDRESS"})

    # referrer 주소 검증 (지정된 경우)
    if body.referrer_address:
        try:
            _validate_solana_address(body.referrer_address, field_name="referrer_address")
        except HTTPException as e:
            raise HTTPException(status_code=422, detail={"error": f"referrer_address error: {e.detail}", "code": "INVALID_ADDRESS"})

    # Step 0b: Privy JWT 선택적 검증 (X-Privy-Token 또는 Authorization: Bearer 모두 허용)
    privy_user_id: Optional[str] = None
    _jwt_token = x_privy_token
    if not _jwt_token and authorization:
        # Authorization: Bearer <token>
        if authorization.startswith("Bearer "):
            _jwt_token = authorization[7:].strip()
    if _jwt_token:
        privy_user_id = _verify_privy_jwt(_jwt_token)
        if privy_user_id:
            logger.info(f"Privy 검증 성공: user_id={privy_user_id}")
        else:
            logger.warning("Privy JWT 검증 실패 — 토큰 무시하고 계속 진행")

    follower = body.follower_address

    # ── Step 0c: JWT 있는 경우 follower_address 소유권 검증 ──
    # Privy JWT의 sub(did:privy:xxx)가 DB의 follower privy_user_id와 일치해야 함
    # 최초 온보딩 시: 토큰만 있으면 통과 (privy_user_id 신규 등록)
    # 재온보딩 시: 기존 privy_user_id와 일치해야 함
    if privy_user_id and _db:
        try:
            async with _db.execute(
                "SELECT privy_user_id FROM followers WHERE address=? AND active=1",
                (follower,)
            ) as cur:
                row = await cur.fetchone()
            if row and row[0] and row[0] != privy_user_id:
                # 이미 다른 Privy 유저가 등록한 주소
                logger.warning(f"Privy ID 불일치: 기존={row[0][:20]} 요청={privy_user_id[:20]}")
                raise HTTPException(
                    status_code=403,
                    detail={"error": "This wallet is registered under a different account.", "code": "ADDRESS_CONFLICT"}
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.debug(f"Privy 소유권 검증 DB 오류 (무시): {e}")
    # risk_mode 트레이더 → preset 트레이더 → 직접 지정 → DEFAULT_TIER1 순
    _preset_traders = preset.get("traders") if preset else None
    traders = body.traders or _risk_traders or _preset_traders or DEFAULT_TIER1

    result = {
        "follower": follower,
        "builder_code_approved": True,   # noivan Builder Code platform-level approval done (2026-03-18)
        "followers_registered": [],
        "monitors_started": [],
        "errors": [],
    }

    # ── Step 1: Builder Code 서명 생성 ────────────────
    timestamp = int(time.time() * 1000)
    payload_to_sign = {
        "timestamp":     timestamp,
        "expiry_window": 5000,
        "type":          "approve_builder_code",
        "data": {
            "builder_code": BUILDER_CODE,
            "max_fee_rate": BUILDER_FEE_RATE,
        },
    }

    try:
        if body.client_signature:
            # Privy embedded wallet이 프론트에서 직접 서명한 경우 (우선)
            signature = body.client_signature
            logger.info(f"Privy 클라이언트 서명 사용: {follower[:12]}...")
        elif body.private_key:
            # ⚠️ DEPRECATED: private_key HTTP body 전송 — 보안 경고
            # 프로덕션에서는 client_signature를 사용해야 합니다
            logger.warning(
                f"[SECURITY] private_key field used in /followers/onboard request "
                f"from follower={follower[:12]}... — "
                f"DEPRECATED: Use client_signature (Privy embedded wallet) instead. "
                f"Sending private keys over HTTP is a security risk."
            )
            # 서버 측 개인키로 서명 (데모/백엔드 용도 — DEPRECATED)
            signature = _sign_builder_approval(body.private_key, payload_to_sign)
        else:
            # 서명 없음 — Builder Code 승인 보류 (Pacifica 팀 등록 후 자동 처리)
            logger.info("서명 미제공 — Builder Code 스킵 (팔로우는 정상 진행)")
            signature = None
    except ImportError:
        logger.warning("solders 없음 — Builder Code 승인 스킵")
        signature = None
    except Exception as e:
        result["errors"].append(f"Signature generation failed: {e}")
        signature = None

    # ── Step 2: Pacifica approve API ──────────────────
    if signature:
        try:
            api_result = _approve_builder_code_api(
                follower, signature, timestamp, AGENT_WALLET
            )
            if api_result.get("success") or api_result.get("ok"):
                result["builder_code_approved"] = True
                logger.info(f"Builder Code 승인 완료: {follower[:12]}...")
            else:
                result["errors"].append(f"API 응답: {api_result}")
        except Exception as e:
            # 승인 실패해도 팔로우는 계속 (수수료 수취만 비활성)
            result["errors"].append(f"Builder Code API error: {e}")

    # ── Step 3: DB 팔로워 등록 ────────────────────────
    if not _db:
        raise HTTPException(
            status_code=503,
            detail={"error": "DB not initialized", "code": "SERVICE_UNAVAILABLE"}
        )
    from db.database import add_trader as _add_trader
    for trader_addr in traders:
        try:
            # traders 테이블에 먼저 등록 (없으면 추가, 있으면 무시)
            await _add_trader(_db, trader_addr)
            await add_follower(
                _db, follower, trader_addr,
                copy_ratio=resolved_copy_ratio,
                max_position_usdc=resolved_max_pos_usdc,
            )
            # builder_code 승인 — noivan 플랫폼 레벨 승인 완료이므로 항상 1
            await _db.execute(
                "UPDATE followers SET builder_code_approved=1, builder_approved=1 WHERE address=?",
                (follower,)
            )
            # privy_user_id 저장 (있을 경우)
            if privy_user_id:
                try:
                    await _db.execute(
                        "UPDATE followers SET privy_user_id=? WHERE address=?",
                        (privy_user_id, follower)
                    )
                    logger.info(f"privy_user_id 저장: {follower[:12]}... → {privy_user_id}")
                except Exception as e:
                    logger.debug(f"privy_user_id 저장 실패 (컬럼 없을 수 있음): {e}")
            await _db.commit()
            result["followers_registered"].append(trader_addr)
            logger.info(f"팔로워 등록: {follower[:12]}... → {trader_addr[:12]}...")
        except Exception as e:
            result["errors"].append(f"DB registration failed {trader_addr[:12]}: {e}")

    # ── Step 4: PositionMonitor 시작 ──────────────────
    for trader_addr in traders:
        if trader_addr not in _monitors and _engine:
            try:
                monitor = RestPositionMonitor(trader_addr, _engine.on_fill)
                _monitors[trader_addr] = monitor
                background_tasks.add_task(monitor.start)
                result["monitors_started"].append(trader_addr)
            except Exception as e:
                result["errors"].append(f"Monitor start failed {trader_addr[:12]}: {e}")

    # ── Step 5: Fuul 레퍼럴 추적 ─────────────────────
    if body.referrer_address and _engine:
        try:
            fuul = FuulReferral()
            await fuul.track_referral(body.referrer_address, follower)
        except Exception as e:
            logger.debug(f"무시된 예외: {e}")

    # ── Step 6: Builder Code 자동 승인 (백그라운드) ──────
    async def _auto_approve_builder(address: str):
        try:
            from pacifica.builder_code import approve
            from solders.keypair import Keypair
            import base58
            pk = os.getenv("AGENT_PRIVATE_KEY", "")
            if pk:
                kp = Keypair.from_seed(base58.b58decode(pk)[:32])
                res = approve(account=address, keypair=kp)
                logger.info(f"Builder Code 자동 승인: {address[:16]} → {res.get('ok')}")
        except Exception as e:
            logger.debug(f"Builder Code 자동 승인 실패 (무시): {e}")

    background_tasks.add_task(_auto_approve_builder, body.follower_address)

    result["ok"] = len(result["followers_registered"]) > 0
    result["note"] = (
        f"Builder Code '{BUILDER_CODE}' {'Approved' if result['builder_code_approved'] else 'Not approved (orders work, fee collection disabled)'}"
    )
    # 적용된 전략 정보 응답에 포함
    # effective_copy_ratio: copy_engine이 실제 적용할 값 (프리셋 상한선 적용)
    # copy_engine.py Round 6 수정: min(user_ratio, preset_ratio) 적용
    _preset_cap_ratio = STRATEGY_PRESETS.get(body.strategy or "safe", {}).get("copy_ratio")
    effective_ratio = (
        min(resolved_copy_ratio, _preset_cap_ratio)
        if _preset_cap_ratio is not None and resolved_copy_ratio > _preset_cap_ratio
        else resolved_copy_ratio
    )
    result["strategy"] = {
        "key":                  body.strategy or "safe",
        "label":                resolved_strategy_label,
        "copy_ratio":           resolved_copy_ratio,      # 유저 입력값 (저장값)
        "effective_copy_ratio": effective_ratio,          # 실제 실행 시 적용값
        "max_position_usdc":    resolved_max_pos_usdc,
        "desc":                 preset.get("desc", ""),
    }
    if privy_user_id:
        result["privy_user_id"] = privy_user_id

    # ── Step 7: Agent 바인딩 안내 ────────────────────────
    # 팔로워가 Pacifica 앱에서 Agent를 바인딩해야 복사 거래가 실행됩니다.
    _network = os.getenv("NETWORK", "testnet")
    _agent_wallet = os.getenv("AGENT_WALLET", "9mxJJAQwKLmM3hUdFebFXgkD8TPnDEJCZWhWN2uLZHWi")
    _binding_url = (
        "https://app.pacifica.fi/settings/agents"
        if _network == "mainnet"
        else f"https://{os.getenv('NETWORK', 'testnet')}.app.pacifica.fi"
    )
    result["agent_binding_required"] = True   # 팔로워가 Pacifica 앱에서 Agent 등록 필요
    result["agent_wallet"] = _agent_wallet
    result["agent_binding_url"] = _binding_url

    return result


@router.get("/strategies")
async def list_strategies() -> dict:
    """
    사용 가능한 전략 프리셋 목록 조회
    GET /followers/strategies
    → 프론트엔드 전략 선택 UI용

    반환 항목:
    - key, label, desc: 표시용
    - copy_ratio, max_position_usdc: 핵심 파라미터
    - risk_level: LOW / MEDIUM / HIGH
    - expected_monthly_roi_pct: $10k 기준 월 예상 ROI (mainnet 실데이터 기반)
    - expected_max_dd_pct: 예상 최대 낙폭
    - traders: 복사 트레이더 목록 (주소 마스킹)
    - is_default: 기본값 여부
    """
    CAPITAL_EXAMPLE = 10_000   # 예시 투자금

    strategies_out = []
    for key, p in STRATEGY_PRESETS.items():
        monthly_roi = p.get("expected_monthly_roi_pct", 0)
        max_dd      = p.get("expected_max_dd_pct", 0)
        monthly_pnl = round(CAPITAL_EXAMPLE * monthly_roi / 100, 2)

        # 트레이더 마스킹 (앞 4자리...끝 4자리)
        traders_masked = [
            f"{addr[:4]}...{addr[-4:]}" for addr in p.get("traders", [])
        ]

        strategies_out.append({
            "key":                    key,
            "label":                  p["label"],
            "desc":                   p["desc"],
            "risk_level":             p.get("risk_level", "MEDIUM"),
            "is_default":             p.get("is_default", False),

            # 핵심 파라미터
            "copy_ratio":             p["copy_ratio"],
            "copy_ratio_pct":         round(p["copy_ratio"] * 100, 0),
            "max_position_usdc":      p["max_position_usdc"],
            "stop_loss_pct":          p["stop_loss_pct"],
            "take_profit_pct":        p["take_profit_pct"],
            "max_open_positions":     p["max_open_positions"],
            "n_traders":              p["n_traders"],

            # 예상 수익 ($10,000 투자 기준, mainnet 2026-03-19 실데이터)
            "example_capital":        CAPITAL_EXAMPLE,
            "expected_monthly_pnl":   monthly_pnl,
            "expected_monthly_roi_pct": monthly_roi,
            "expected_max_dd_pct":    max_dd,

            # 트레이더 정보
            "traders_masked":         traders_masked,
            "trader_count":           len(p.get("traders", [])),
        })

    return {
        "strategies":   strategies_out,
        "default":      "default",
        "strategy_count": len(strategies_out),
        "data_source":  "Hyperliquid Mainnet leaderboard (2026-03-19 snapshot)",
        "realism_factor": 0.82,
        "note":         (
            "expected_monthly_pnl is estimated from mainnet real data. "
            "Slippage (18%) and fees (0.15%/trade) are factored in (conservative). "
            "Past performance does not guarantee future returns."
        ),
    }


@router.get("/list")
async def list_followers(follower_address: Optional[str] = None) -> dict:
    """팔로워 목록 조회 — follower_address로 본인 데이터만 조회"""
    from api.deps import _get_db_direct
    _db_local = _get_db_direct()  # 한 번만 호출 → 로컬 변수에 저장 (이중 호출 방지)
    if not _db_local:
        raise HTTPException(503, "DB not initialized")
    # 빈 문자열 조기 반환 (DB 전체 쿼리 방지)
    if follower_address is not None and follower_address.strip() == "":
        return {"data": [], "count": 0}
    if follower_address:
        # 주소 형식 검증 (did:privy: 허용)
        if not follower_address.startswith("did:privy:") and not _SOLANA_ADDR_RE.match(follower_address):
            raise HTTPException(
                status_code=422,
                detail={"error": "Invalid Solana address format", "code": "INVALID_ADDRESS"}
            )
        # 본인 주소에 해당하는 팔로워 데이터만 반환
        async with _db_local.execute(
            "SELECT * FROM followers WHERE address=? AND active=1 ORDER BY created_at DESC",
            (follower_address,)
        ) as cur:
            rows = await cur.fetchall()
    else:
        # follower_address 미제공 시 빈 목록 반환 (전체 조회 불허)
        return {"data": [], "count": 0, "note": "follower_address parameter is required"}
    return {"data": [dict(r) for r in rows], "count": len(rows)}


@router.get("/{follower_address}/pnl")
async def get_follower_pnl(follower_address: str) -> dict:
    """
    팔로워 누적 PnL 집계
    - realized_pnl_usdc: SUM(pnl) FROM copy_trades (pnl IS NOT NULL)
    - unrealized_pnl_usdc: follower_positions 기반 (mark_price 없으면 0)
    - win_trades / lose_trades / win_rate
    - total_volume_usdc: SUM(amount * price) FROM copy_trades (status=filled)
    - builder_fee_paid: SUM(fee_usdc) FROM fee_records
    - open_positions: follower_positions 목록
    - pnl_by_trader: 트레이더별 PnL 집계
    """
    from api.deps import _get_db_direct
    _db = _get_db_direct()
    if not _db:
        raise HTTPException(
            status_code=503,
            detail={"error": "DB not initialized", "code": "SERVICE_UNAVAILABLE"}
        )

    # ── 실현 PnL ──────────────────────────────────────────
    async with _db.execute(
        "SELECT COALESCE(SUM(pnl), 0) FROM copy_trades WHERE follower_address=? AND pnl IS NOT NULL",
        (follower_address,),
    ) as cur:
        realized_pnl = float((await cur.fetchone())[0])

    # ── 총 거래 수 / 승 / 패 ──────────────────────────────
    async with _db.execute(
        "SELECT COUNT(*) FROM copy_trades WHERE follower_address=?",
        (follower_address,),
    ) as cur:
        total_trades = int((await cur.fetchone())[0])

    async with _db.execute(
        "SELECT COUNT(*) FROM copy_trades WHERE follower_address=? AND pnl > 0",
        (follower_address,),
    ) as cur:
        win_trades = int((await cur.fetchone())[0])

    async with _db.execute(
        "SELECT COUNT(*) FROM copy_trades WHERE follower_address=? AND pnl < 0",
        (follower_address,),
    ) as cur:
        lose_trades = int((await cur.fetchone())[0])

    win_rate = round(win_trades / max(win_trades + lose_trades, 1) * 100, 2) if (win_trades + lose_trades) > 0 else 0.0

    # ── 총 거래 볼륨 ─────────────────────────────────────
    async with _db.execute(
        """SELECT COALESCE(SUM(CAST(amount AS REAL) * CAST(price AS REAL)), 0)
           FROM copy_trades WHERE follower_address=? AND status='filled'""",
        (follower_address,),
    ) as cur:
        total_volume = float((await cur.fetchone())[0])

    # ── Builder Fee ────────────────────────────────────────
    async with _db.execute(
        """SELECT COALESCE(SUM(fr.fee_usdc), 0)
           FROM fee_records fr
           JOIN copy_trades ct ON fr.trade_id = ct.id
           WHERE ct.follower_address=?""",
        (follower_address,),
    ) as cur:
        builder_fee_paid = float((await cur.fetchone())[0])

    # ── 열린 포지션 목록 ─────────────────────────────────
    from db.database import get_all_follower_positions
    open_positions = await get_all_follower_positions(_db, follower_address)

    # P0 Fix (Round 5): unrealized PnL — DataCollector 마크 가격 캐시 활용
    # mark_price 없으면 0 처리 (기존 동작 유지)
    unrealized_pnl = 0.0
    try:
        from core.data_collector import get_price_cache
        _price_cache = get_price_cache()
        for _pos in open_positions:
            _sym = _pos.get("symbol", "").upper()
            _entry = float(_pos.get("entry_price", 0) or 0)
            _size = float(_pos.get("size", 0) or 0)
            _side = _pos.get("side", "bid")
            _mkt = _price_cache.get(_sym, {})
            _mark = float(_mkt.get("mark", 0) or 0)
            if _mark > 0 and _entry > 0 and _size > 0:
                if _side == "bid":
                    _upnl = (_mark - _entry) * _size
                else:  # ask (숏)
                    _upnl = (_entry - _mark) * _size
                unrealized_pnl += _upnl
                _pos["mark_price"] = _mark        # 응답에 마크 가격 포함
                _pos["unrealized_pnl"] = round(_upnl, 6)
            else:
                _pos["mark_price"] = None
                _pos["unrealized_pnl"] = 0.0
        unrealized_pnl = round(unrealized_pnl, 4)
    except Exception as _upnl_e:
        logger.debug(f"[PnL] unrealized PnL 계산 오류 (무시): {_upnl_e}")
        unrealized_pnl = 0.0

    # ── 트레이더별 PnL ────────────────────────────────────
    async with _db.execute(
        """SELECT trader_address, COALESCE(SUM(pnl), 0) as pnl_sum, COUNT(*) as cnt
           FROM copy_trades WHERE follower_address=? AND pnl IS NOT NULL
           GROUP BY trader_address""",
        (follower_address,),
    ) as cur:
        rows = await cur.fetchall()
    pnl_by_trader = [
        {"trader_address": dict(r)["trader_address"], "pnl_usdc": round(dict(r)["pnl_sum"], 4), "trades": dict(r)["cnt"]}
        for r in rows
    ]

    # ── ROI (초기 자산 추정 불가 → 0) ────────────────────
    roi_pct = 0.0

    return {
        "follower_address": follower_address,
        "realized_pnl_usdc": round(realized_pnl, 4),
        "unrealized_pnl_usdc": round(unrealized_pnl, 4),
        "total_trades": total_trades,
        "win_trades": win_trades,
        "lose_trades": lose_trades,
        "win_rate": win_rate,
        "total_volume_usdc": round(total_volume, 2),
        "open_positions": open_positions,
        "pnl_by_trader": pnl_by_trader,
        "roi_pct": roi_pct,
        "builder_fee_paid": round(builder_fee_paid, 6),
    }


@router.delete("/{trader_address}")
async def remove_follower(
    trader_address: str,
    follower_address: Optional[str] = Query(default=None, description="팔로워 지갑 주소"),
    request: Request = None,
    x_privy_token: Optional[str] = Header(None, alias="X-Privy-Token"),
    authorization: Optional[str] = Header(None, alias="Authorization"),
) -> dict:
    """
    팔로우 해지 (soft delete)

    호출 방식:
      DELETE /followers/{trader_address}?follower_address={follower_wallet}

    follower_address 제공 시: 해당 팔로워-트레이더 쌍만 해지
    미제공 시: trader_address를 follower로 간주 (backward compat)

    ⚠️ 보안 주의 (Round 4):
    follower_address가 쿼리 파라미터로만 전달 — 현재 누구나 타인 해지 가능.
    강화된 rate limit (분당 10회) 적용.
    Privy JWT 있을 경우 소유권 검증 수행.
    TODO: Privy JWT 필수화 (v2 계획)
    """
    from api.deps import _get_db_direct
    from api.main import _check_rate_limit

    # ── Rate limit 강화: IP당 분당 10회 (기본 unfollow 정책 사용) ──────
    _client_ip = request.client.host if request and request.client else "unknown"
    _rl_key = f"unfollow:{_client_ip}"
    if not _check_rate_limit(_rl_key, max_calls=10, window_sec=60):
        raise HTTPException(
            status_code=429,
            headers={"Retry-After": "60"},
            detail={"error": "Rate limit exceeded", "code": "RATE_LIMIT_EXCEEDED", "retry_after_seconds": 60}
        )

    _db = _get_db_direct()
    if not _db:
        raise HTTPException(
            status_code=503,
            detail={"error": "DB not initialized", "code": "SERVICE_UNAVAILABLE"}
        )

    # ── Privy JWT 선택적 소유권 검증 ──────────────────────────────────
    # JWT가 제공된 경우: follower_address 소유권 확인 (타인 해지 방지)
    _jwt_token = x_privy_token
    if not _jwt_token and authorization and authorization.startswith("Bearer "):
        _jwt_token = authorization[7:].strip()

    if _jwt_token and follower_address:
        _privy_uid = _verify_privy_jwt(_jwt_token)
        if _privy_uid:
            # DB에서 해당 팔로워의 privy_user_id 확인
            try:
                async with _db.execute(
                    "SELECT privy_user_id FROM followers WHERE address=? AND active=1 LIMIT 1",
                    (follower_address,)
                ) as cur:
                    row = await cur.fetchone()
                if row and row[0] and row[0] != _privy_uid:
                    logger.warning(
                        f"[SECURITY] DELETE /followers/{trader_address[:12]} — "
                        f"Privy ID mismatch: requester={_privy_uid[:20]}, "
                        f"owner={row[0][:20]}, follower={follower_address[:12]}"
                    )
                    raise HTTPException(
                        status_code=403,
                        detail={"error": "Not authorized to remove this follower", "code": "FORBIDDEN"}
                    )
            except HTTPException:
                raise
            except Exception as _ve:
                logger.debug(f"DELETE follower Privy 검증 DB 오류 (무시): {_ve}")
        else:
            logger.warning(f"[SECURITY] DELETE /followers — invalid Privy JWT provided, proceeding without auth")
    elif not _jwt_token and follower_address:
        # JWT 없이 follower_address 쿼리 파라미터로만 요청 — 보안 감사 로그
        logger.warning(
            f"[SECURITY] DELETE /followers/{trader_address[:12]} "
            f"without auth token. follower={follower_address[:12] if follower_address else 'N/A'} "
            f"from ip={_client_ip}. "
            f"TODO: Require Privy JWT in v2."
        )

    if follower_address:
        # 주소 검증
        if not follower_address.startswith("did:privy:") and not _SOLANA_ADDR_RE.match(follower_address):
            raise HTTPException(422, {"error": "Invalid follower_address", "code": "INVALID_ADDRESS"})
        # 팔로워 + 트레이더 쌍 해지
        await _db.execute(
            "UPDATE followers SET active=0 WHERE address=? AND trader_address=?",
            (follower_address, trader_address)
        )
    else:
        # backward compat: path param이 follower address인 경우
        await _db.execute(
            "UPDATE followers SET active=0 WHERE address=?", (trader_address,)
        )
    await _db.commit()
    return {"ok": True, "follower": trader_address, "status": "removed"}


@router.get("/presets")
async def get_risk_presets():
    """사용 가능한 리스크 프리셋 목록 반환 — 프론트엔드 시나리오 선택 UI용"""
    _labels = {
        "default":      "Default",
        "conservative": "Conservative",
        "balanced":     "Balanced",
        "aggressive":   "Aggressive",
    }
    return {
        "presets": [
            {
                "mode":                  k,
                "label":                 _labels.get(k, k),
                "trader_count":          len(v["traders"]),
                "copy_ratio_pct":        v["copy_ratio"] * 100,
                "max_position_usdc":     v["max_position_usdc"],
                "description":           v["description"],
                "expected_monthly_roi_pct": v["expected_monthly_roi_pct"],
            }
            for k, v in RISK_PRESETS.items()
        ]
    }



@router.get("/paper-trading")
async def get_paper_trading():
    """
    4가지 전략 병렬 페이퍼트레이딩 실시간 현황
    paper_perp.db (별도 파일) 기반 — paper_trading_4x.py 엔진이 실시간 기록
    """
    import aiosqlite as _aiosqlite
    import os as _os
    _ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    paper_db = _os.path.join(_ROOT, "paper_perp.db")

    if not _os.path.exists(paper_db):
        return {
            "status":  "not_started",
            "message": "Paper trading not started. Run: python3 scripts/paper_trading_4x.py --capital 10000",
            "comparison": [],
        }

    STRATEGY_META = {
        "default":      {"label": "📋 Default",  "expected_30d": 13.7, "traders": 3, "copy_ratio": 0.10, "max_pos": 100},
        "conservative": {"label": "🛡️ Safe", "expected_30d":  4.2, "traders": 2, "copy_ratio": 0.10, "max_pos":  50},
        "balanced":     {"label": "⚖️ Balanced",  "expected_30d": 11.4, "traders": 5, "copy_ratio": 0.10, "max_pos": 100},
        "aggressive":   {"label": "🚀 Aggressive",  "expected_30d": 23.6, "traders": 5, "copy_ratio": 0.15, "max_pos": 200},
    }

    try:
        db = await _aiosqlite.connect(paper_db)
        db.row_factory = _aiosqlite.Row

        # 세션 현황
        async with db.execute("SELECT * FROM paper_sessions ORDER BY strategy") as cur:
            session_rows = await cur.fetchall()
        sessions = [dict(r) for r in session_rows]

        if not sessions:
            await db.close()
            return {
                "status":  "not_started",
                "message": "No session. Run: python3 scripts/paper_trading_4x.py --capital 10000",
                "comparison": [],
            }

        # 최근 24h 스냅샷 히스토리 (전략별)
        cutoff = int((time.time() - 86400) * 1000)
        history = {}
        for s in sessions:
            strat = s["strategy"]
            async with db.execute(
                """SELECT snapshot_at, equity, realized_pnl, unrealized_pnl, win_rate
                   FROM paper_snapshots WHERE strategy=? AND snapshot_at>?
                   ORDER BY snapshot_at""",
                (strat, cutoff)
            ) as cur:
                rows = await cur.fetchall()
            history[strat] = [
                {"ts": r[0], "equity": r[1], "realized_pnl": r[2],
                 "unrealized_pnl": r[3], "win_rate": r[4]}
                for r in rows
            ]

        # 전략별 최근 체결 거래 (최대 5건)
        recent_trades = {}
        for s in sessions:
            strat = s["strategy"]
            async with db.execute(
                """SELECT symbol, side, action, pnl, roi_pct, hold_min
                   FROM paper_trades WHERE session_strategy=? AND action='close'
                   ORDER BY closed_at DESC LIMIT 5""",
                (strat,)
            ) as cur:
                rows = await cur.fetchall()
            recent_trades[strat] = [
                {"symbol": r[0], "side": r[1], "pnl": r[3], "roi_pct": r[4], "hold_min": r[5]}
                for r in rows
            ]

        # 전체 거래량 합산
        async with db.execute(
            "SELECT COUNT(*), SUM(ABS(pnl)) FROM paper_trades WHERE action='close'"
        ) as cur:
            agg = await cur.fetchone()
        total_closed = agg[0] or 0
        total_vol    = agg[1] or 0.0

        await db.close()

    except Exception as e:
        return {"status": "error", "message": str(e), "comparison": []}

    # 비교 테이블 구성
    comparison = []
    for s in sessions:
        strat = s["strategy"]
        meta  = STRATEGY_META.get(strat, {})
        cap   = s["initial_capital"] or 10000
        eq    = s["current_equity"]
        roi   = (eq - cap) / cap * 100 if cap else 0
        total = s["win_trades"] + s["lose_trades"]
        wr    = s["win_trades"] / total * 100 if total else 0
        days  = max(1, (int(time.time() * 1000) - s["started_at"]) / 86400000)

        comparison.append({
            "strategy":          strat,
            "label":             meta.get("label", strat),
            "copy_ratio_pct":    meta.get("copy_ratio", 0.10) * 100,
            "max_pos_usdc":      meta.get("max_pos", 100),
            "n_traders":         meta.get("traders", 0),
            "initial_capital":   cap,
            "current_equity":    round(eq, 2),
            "realized_pnl":      round(s["realized_pnl"], 2),
            "unrealized_pnl":    round(s["unrealized_pnl"], 2),
            "total_pnl":         round(s["realized_pnl"] + s["unrealized_pnl"], 2),
            "roi_pct":           round(roi, 4),
            "roi_annualized_pct":round(roi / days * 365, 2),
            "win_rate":          round(wr, 2),
            "wins":              s["win_trades"],
            "losses":            s["lose_trades"],
            "total_trades":      total,
            "expected_30d_roi":  meta.get("expected_30d", 0),
            "vs_expected_pct":   round(roi - meta.get("expected_30d", 0), 2),
            "days_running":      round(days, 2),
            "snapshot_count":    len(history.get(strat, [])),
            "recent_trades":     recent_trades.get(strat, []),
        })

    # 순위 (ROI 기준)
    comparison_sorted = sorted(comparison, key=lambda x: x["roi_pct"], reverse=True)

    return {
        "status":               "running",
        "last_updated":         datetime.now(timezone.utc).isoformat(),
        "comparison":           comparison,
        "ranking":              [c["strategy"] for c in comparison_sorted],
        "history":              history,
        "total_closed_trades":  total_closed,
        "total_paper_volume":   round(total_vol, 2),
        "builder_fee_simulated":round(total_vol * 0.001, 4),
        "engine_script":        "scripts/paper_trading_4x.py",
        "data_source":          "Mainnet trader position tracking (60s polling)",
        "note": (
            "Paper trading: detects position changes without placing real orders. "
            "Virtual entry/exit simulation with 0.05% slippage + 0.06% taker fee."
        ),
    }


# ── 포트폴리오 (pnl alias + followed traders) ─────────────────────────────
@router.get("/{follower_address}/portfolio")
async def get_follower_portfolio(follower_address: str) -> dict:
    """
    팔로워 포트폴리오 전체 조회
    - realized/unrealized PnL
    - 팔로우 중인 트레이더 목록
    - 거래 통계
    """
    from api.deps import _get_db_direct
    _db = _get_db_direct()
    if not _db:
        raise HTTPException(status_code=503, detail={"error": "DB not initialized", "code": "SERVICE_UNAVAILABLE"})

    if not _SOLANA_ADDR_RE.match(follower_address):
        raise HTTPException(422, detail={"error": "Invalid Solana address", "code": "INVALID_ADDRESS"})

    # PnL 데이터
    async with _db.execute(
        "SELECT COALESCE(SUM(pnl), 0) FROM copy_trades WHERE follower_address=? AND pnl IS NOT NULL",
        (follower_address,)
    ) as cur:
        realized_pnl = (await cur.fetchone())[0] or 0.0

    async with _db.execute(
        "SELECT COUNT(*) FROM copy_trades WHERE follower_address=?",
        (follower_address,)
    ) as cur:
        total_trades = (await cur.fetchone())[0] or 0

    async with _db.execute(
        "SELECT COUNT(*) FROM copy_trades WHERE follower_address=? AND pnl > 0",
        (follower_address,)
    ) as cur:
        win_trades = (await cur.fetchone())[0] or 0

    async with _db.execute(
        "SELECT COUNT(*) FROM copy_trades WHERE follower_address=? AND pnl < 0",
        (follower_address,)
    ) as cur:
        lose_trades = (await cur.fetchone())[0] or 0

    async with _db.execute(
        "SELECT COALESCE(SUM(amount * price), 0) FROM copy_trades WHERE follower_address=? AND status='filled'",
        (follower_address,)
    ) as cur:
        total_volume = (await cur.fetchone())[0] or 0.0

    # 팔로우 중인 트레이더 목록
    async with _db.execute(
        """SELECT f.trader_address, f.copy_ratio, f.max_position_usdc, f.created_at,
                  t.win_rate, t.pnl_30d, t.roi_30d
           FROM followers f
           LEFT JOIN traders t ON f.trader_address = t.address
           WHERE f.address=? AND f.active=1""",
        (follower_address,)
    ) as cur:
        rows = await cur.fetchall()

    followed_traders = []
    for r in rows:
        followed_traders.append({
            "trader_address": r[0],
            "copy_ratio": r[1],
            "max_position_usdc": r[2],
            "followed_at": r[3],
            "trader_win_rate": r[4],
            "trader_pnl_30d": r[5],
            "trader_roi_30d": r[6],
        })

    # 최근 거래 5건
    async with _db.execute(
        """SELECT symbol, side, status, pnl, created_at, trader_address
           FROM copy_trades WHERE follower_address=?
           ORDER BY created_at DESC LIMIT 5""",
        (follower_address,)
    ) as cur:
        recent_rows = await cur.fetchall()

    recent_trades = [
        {
            "symbol": r[0], "side": r[1], "status": r[2],
            "pnl": r[3], "created_at": r[4], "trader": r[5]
        }
        for r in recent_rows
    ]

    win_rate = win_trades / total_trades * 100 if total_trades > 0 else 0.0

    return {
        "ok": True,
        "follower_address": follower_address,
        "pnl": {
            "realized_usdc": round(realized_pnl, 4),
            "unrealized_usdc": 0.0,
            "total_usdc": round(realized_pnl, 4),
        },
        "stats": {
            "total_trades": total_trades,
            "win_trades": win_trades,
            "lose_trades": lose_trades,
            "win_rate_pct": round(win_rate, 1),
            "total_volume_usdc": round(total_volume, 2),
        },
        "followed_traders": followed_traders,
        "followed_count": len(followed_traders),
        "recent_trades": recent_trades,
    }
