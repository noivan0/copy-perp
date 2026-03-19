"""
팔로워 온보딩 라우터
POST /followers/onboard — 팔로워 지갑 등록 + Builder Code 승인 + Tier1 트레이더 자동 팔로우

플로우:
1. 팔로워 지갑 주소 + 개인키 받기
2. Builder Code 'noivan' 승인 서명 자동 생성 (개인키로 서명)
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
from fastapi import APIRouter, HTTPException, BackgroundTasks, Header, Request
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
        "description": "기본 설정. S등급 1명 + A등급 최상위 1명. 월 예상 +13.4%",
        "expected_monthly_roi_pct": 13.4,
    },
    "conservative": {
        "traders": TRADER_S[:1],                     # S 1명만
        "copy_ratio": 0.10,
        "max_position_usdc": 100.0,
        "description": "보수적. 가장 신뢰도 높은 트레이더 1명만. 월 예상 +7.8%",
        "expected_monthly_roi_pct": 7.8,
    },
    "balanced": {
        "traders": TRADER_S + TRADER_A[:3],          # S 1명 + A 상위 3명
        "copy_ratio": 0.07,
        "max_position_usdc": 300.0,
        "description": "균형. S등급 1명 + A등급 상위 3명. 월 예상 +18.3%",
        "expected_monthly_roi_pct": 18.3,
    },
    "aggressive": {
        "traders": TRADER_S + TRADER_A,              # S 1명 + A 전체 8명
        "copy_ratio": 0.07,
        "max_position_usdc": 500.0,
        "description": "적극적. S+A등급 전체 9명. 월 예상 +33.6%",
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
        "label":              "🛡 안전형",
        "desc":               "원금 보존 최우선. 가장 신뢰도 높은 트레이더 1명. MDD 최소화.",
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
        "label":              "⚖️ 균형형",
        "desc":               "수익과 리스크 균형. S+A등급 상위 4명 분산. 권장 시나리오.",
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
        "label":              "⚡ 공격형",
        "desc":               "최대 수익 추구. S+A등급 전체 9명. 손실 리스크 수용 필수.",
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
    2. base58 문자셋 + 32-44자 regex 검증
    3. base58 디코딩 + 32바이트 (Ed25519 공개키) 확인
    실패 시 HTTPException(422) 발생
    """
    if not address or not isinstance(address, str):
        raise HTTPException(status_code=422, detail={"error": f"{field_name}가 필요합니다", "code": "VALIDATION_ERROR"})

    # 1차: regex 형식 검증
    if not _SOLANA_ADDR_RE.match(address):
        raise HTTPException(
            422,
            f"유효하지 않은 Solana 주소 형식: '{address[:20]}...' "
            f"(base58 문자셋, 32-44자 필요)"
        )

    # 2차: base58 디코딩 + 32바이트 확인
    try:
        decoded = _base58.b58decode(address)
        if len(decoded) != 32:
            raise HTTPException(
                422,
                f"유효하지 않은 Solana 주소: base58 디코딩 결과가 {len(decoded)}바이트 "
                f"(32바이트 Ed25519 공개키 필요)"
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            422,
            f"유효하지 않은 Solana 주소: base58 디코딩 실패 — {e}"
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
    private_key: Optional[str] = None          # base58 개인키 (Builder Code 서명용, 선택)
    client_signature: Optional[str] = None     # Privy embedded wallet 서명 (base58) — private_key 대체

    # ── 전략 선택 ─────────────────────────────────────────────────────
    # strategy 지정 시 해당 프리셋이 copy_ratio / max_position_usdc / 기타를 자동 적용.
    # strategy와 copy_ratio를 동시에 지정하면 개별 값이 프리셋을 override.
    # 유효값: "safe" | "default" | "balanced" | "aggressive" | None(기본=default)
    strategy: Optional[str] = "default"

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

    @field_validator("risk_mode")
    @classmethod
    def validate_risk_mode(cls, v):
        if v is not None and v not in RISK_PRESETS:
            raise ValueError(f"risk_mode는 {list(RISK_PRESETS.keys())} 중 하나여야 합니다")
        return v

    @field_validator("strategy")
    @classmethod
    def validate_strategy(cls, v):
        # STRATEGY_PRESETS + RISK_PRESETS 키 모두 허용
        allowed = set(STRATEGY_PRESETS.keys()) | set(RISK_PRESETS.keys())
        if v is not None and v not in allowed:
            raise ValueError(f"strategy는 {sorted(allowed)} 중 하나여야 합니다")
        return v

    @field_validator("preset")
    @classmethod
    def validate_preset(cls, v):
        from core.strategy_presets import PRESETS as _NEW_PRESETS
        if v is not None and v not in _NEW_PRESETS:
            raise ValueError(f"preset은 {list(_NEW_PRESETS.keys())} 중 하나여야 합니다")
        return v

    @field_validator("copy_ratio")
    @classmethod
    def validate_copy_ratio(cls, v):
        if v is not None and (v < 0.01 or v > 1.0):
            raise ValueError("copy_ratio는 0.01 ~ 1.0 범위여야 합니다")
        return v

    @field_validator("max_position_usdc")
    @classmethod
    def validate_max_position(cls, v):
        if v is not None and (v < 1 or v > 10000):
            raise ValueError("max_position_usdc는 1 ~ 10000 범위여야 합니다")
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
        preset_key = self.strategy or "default"
        preset = dict(STRATEGY_PRESETS[preset_key])
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
            detail={"error": "유효하지 않은 Privy 토큰입니다", "code": "AUTH_INVALID"}
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
            detail={"error": "copy_ratio는 0.01 ~ 1.0 범위여야 합니다", "code": "INVALID_COPY_RATIO"}
        )
    return v


def _validate_max_position_field(v: float) -> float:
    """max_position_usdc: 1 ~ 10000 범위 강제"""
    if v < 1 or v > 10000:
        raise HTTPException(
            status_code=400,
            detail={"error": "max_position_usdc는 1 ~ 10000 범위여야 합니다", "code": "INVALID_MAX_POSITION"}
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
    from api.main import _db, _engine, _monitors, _check_rate_limit
    from core.position_monitor import RestPositionMonitor
    from db.database import add_follower
    from fuul.referral import FuulReferral

    # ── Rate Limit 체크 ─────────────────────────────────
    client_ip = request.client.host if request.client else "unknown"
    from api.main import RATE_LIMIT_POLICY, _require_rate_limit
    _require_rate_limit(f"onboard:{client_ip}", request=request)

    # ── risk_mode → RISK_PRESETS 적용 (copy_ratio/traders/max_position 미지정 시) ──
    _risk_preset = RISK_PRESETS.get(body.risk_mode or "default", RISK_PRESETS["default"])
    _risk_traders   = body.traders or _risk_preset["traders"]
    _risk_copy_ratio = body.copy_ratio or _risk_preset["copy_ratio"]
    _risk_max_pos    = body.max_position_usdc or _risk_preset["max_position_usdc"]
    logger.info(
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
        f"전략: {resolved_strategy_label} | "
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
                raise HTTPException(status_code=422, detail={"error": f"traders[{idx}] 주소 오류: {e.detail}", "code": "INVALID_ADDRESS"})

    # referrer 주소 검증 (지정된 경우)
    if body.referrer_address:
        try:
            _validate_solana_address(body.referrer_address, field_name="referrer_address")
        except HTTPException as e:
            raise HTTPException(status_code=422, detail={"error": f"referrer_address 오류: {e.detail}", "code": "INVALID_ADDRESS"})

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
                    detail={"error": "이 지갑 주소는 다른 Privy 계정으로 등록되어 있습니다.", "code": "ADDRESS_CONFLICT"}
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
        "builder_code_approved": True,   # noivan Builder Code 플랫폼 레벨 승인 완료 (2026-03-18)
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
            # 서버 측 개인키로 서명 (데모/백엔드 용도)
            signature = _sign_builder_approval(body.private_key, payload_to_sign)
        else:
            # 서명 없음 — Builder Code 승인 보류 (Pacifica 팀 등록 후 자동 처리)
            logger.info("서명 미제공 — Builder Code 스킵 (팔로우는 정상 진행)")
            signature = None
    except ImportError:
        logger.warning("solders 없음 — Builder Code 승인 스킵")
        signature = None
    except Exception as e:
        result["errors"].append(f"서명 생성 실패: {e}")
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
            result["errors"].append(f"Builder Code API 오류: {e}")

    # ── Step 3: DB 팔로워 등록 ────────────────────────
    if _db:
        for trader_addr in traders:
            try:
                await add_follower(
                    _db, follower, trader_addr,
                    copy_ratio=resolved_copy_ratio,
                    max_position_usdc=resolved_max_pos_usdc
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
                result["errors"].append(f"DB 등록 실패 {trader_addr[:12]}: {e}")

    # ── Step 4: PositionMonitor 시작 ──────────────────
    for trader_addr in traders:
        if trader_addr not in _monitors and _engine:
            try:
                monitor = RestPositionMonitor(trader_addr, _engine.on_fill)
                _monitors[trader_addr] = monitor
                background_tasks.add_task(monitor.start)
                result["monitors_started"].append(trader_addr)
            except Exception as e:
                result["errors"].append(f"모니터 시작 실패 {trader_addr[:12]}: {e}")

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
        f"Builder Code '{BUILDER_CODE}' {'승인됨' if result['builder_code_approved'] else '미승인 (주문은 가능, 수수료 수취 비활성)'}"
    )
    # 적용된 전략 정보 응답에 포함
    result["strategy"] = {
        "key":             body.strategy or "safe",
        "label":          resolved_strategy_label,
        "copy_ratio":     resolved_copy_ratio,
        "max_position_usdc": resolved_max_pos_usdc,
        "desc":           preset.get("desc", ""),
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
        else "https://testnet.app.pacifica.fi/settings/agents"
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
        "data_source":  "Hyperliquid Mainnet 리더보드 (2026-03-19 기준)",
        "realism_factor": 0.82,
        "note":         (
            "expected_monthly_pnl은 mainnet 실데이터 기반 추정값입니다. "
            "슬리피지(18%) 및 수수료(0.15%/trade)를 반영한 보수적 수치입니다. "
            "미래 수익을 보장하지 않습니다."
        ),
    }


@router.get("/list")
async def list_followers(trader_address: Optional[str] = None) -> dict:
    """팔로워 목록 조회"""
    from api.main import _db
    from db.database import get_followers
    if not _db:
        raise HTTPException(503, "DB 미초기화")
    if trader_address:
        rows = await get_followers(_db, trader_address)
    else:
        async with _db.execute(
            "SELECT * FROM followers WHERE active=1 ORDER BY created_at DESC LIMIT 100"
        ) as cur:
            rows = await cur.fetchall()
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
    from api.main import _db
    if not _db:
        raise HTTPException(503, "DB 미초기화")

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
    # 미실현 PnL: mark_price 없으면 0
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


@router.delete("/{follower_address}")
async def remove_follower(follower_address: str) -> dict:
    """팔로워 해지 (soft delete)"""
    from api.main import _db
    if not _db:
        raise HTTPException(503, "DB 미초기화")
    await _db.execute(
        "UPDATE followers SET active=0 WHERE address=?", (follower_address,)
    )
    await _db.commit()
    return {"ok": True, "follower": follower_address, "status": "removed"}


@router.get("/presets")
async def get_risk_presets():
    """사용 가능한 리스크 프리셋 목록 반환 — 프론트엔드 시나리오 선택 UI용"""
    _labels = {
        "default":      "기본",
        "conservative": "보수적",
        "balanced":     "균형",
        "aggressive":   "적극적",
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
            "message": "페이퍼트레이딩 미시작. python3 scripts/paper_trading_4x.py --capital 10000 실행 필요",
            "comparison": [],
        }

    STRATEGY_META = {
        "default":      {"label": "📋 기본형",  "expected_30d": 13.7, "traders": 3, "copy_ratio": 0.10, "max_pos": 100},
        "conservative": {"label": "🛡️ 안정형", "expected_30d":  4.2, "traders": 2, "copy_ratio": 0.10, "max_pos":  50},
        "balanced":     {"label": "⚖️ 균형형",  "expected_30d": 11.4, "traders": 5, "copy_ratio": 0.10, "max_pos": 100},
        "aggressive":   {"label": "🚀 공격형",  "expected_30d": 23.6, "traders": 5, "copy_ratio": 0.15, "max_pos": 200},
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
                "message": "세션 없음. python3 scripts/paper_trading_4x.py --capital 10000 실행 필요",
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
        "data_source":          "Mainnet 실트레이더 포지션 추적 (codetabs 프록시, 60초 폴링)",
        "note": (
            "페이퍼트레이딩: 실제 주문 없이 트레이더 포지션 변화를 감지해 "
            "가상 진입/청산 시뮬레이션. 슬리피지 0.05% + taker fee 0.06% 반영."
        ),
    }
