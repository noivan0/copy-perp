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
import json
import base64
import logging
from typing import Optional

import base58 as _base58
from fastapi import APIRouter, HTTPException, BackgroundTasks, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/followers", tags=["followers"])

BUILDER_CODE     = os.getenv("BUILDER_CODE", "noivan")
BUILDER_FEE_RATE = os.getenv("BUILDER_FEE_RATE", "0.0001")
AGENT_WALLET     = os.getenv("AGENT_WALLET", "")

# 기본 팔로우 대상 Tier1 트레이더 (점수 상위)
DEFAULT_TIER1 = [
    "EcX5xSDT45Nvhi2gMTjTnhF3KT2w4sPF54esEZS3hwZu",  # 점수 1위 ROI 82.5%
    "4UBH19qUbXEaqyz9fKrFHuvj8BPMoM87H71s1YPKyGYq",   # Win 100%
]
DEFAULT_COPY_RATIO    = 0.05   # 5% (안전 초기값)
DEFAULT_MAX_POS_USDC  = 50.0  # $50


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
        raise HTTPException(422, f"{field_name}가 필요합니다")

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
    Privy JWT 토큰 검증 (선택적).
    반환: privy_user_id (str) 또는 None (검증 실패)
    
    실제 환경에서는 Privy의 공개키로 JWT를 검증해야 함.
    현재는 JWT 구조 파싱 + did:privy: prefix 확인으로 기본 검증.
    """
    try:
        import base64 as _b64
        # JWT = header.payload.signature
        parts = token.split(".")
        if len(parts) != 3:
            return None
        # payload 디코딩 (base64url)
        payload_b64 = parts[1]
        # padding 추가
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload_bytes = _b64.urlsafe_b64decode(payload_b64)
        payload = json.loads(payload_bytes.decode("utf-8"))
        # sub 필드: "did:privy:..." 형식
        sub = payload.get("sub", "")
        if sub.startswith("did:privy:"):
            return sub  # privy_user_id로 사용
        # 또는 user_id 필드 직접 확인
        user_id = payload.get("user_id") or payload.get("userId")
        if user_id:
            return str(user_id)
        return sub if sub else None
    except Exception as e:
        logger.debug(f"Privy JWT 파싱 실패: {e}")
        return None


class OnboardRequest(BaseModel):
    """팔로워 온보딩 요청"""
    follower_address: str                       # 팔로워 Solana 지갑 주소
    private_key: Optional[str] = None          # base58 개인키 (Builder Code 서명용, 선택)
    client_signature: Optional[str] = None     # Privy embedded wallet 서명 (base58) — private_key 대체
    copy_ratio: float = DEFAULT_COPY_RATIO
    max_position_usdc: float = DEFAULT_MAX_POS_USDC
    referrer_address: Optional[str] = None
    traders: Optional[list] = None             # 지정 시 해당 트레이더만, None이면 DEFAULT_TIER1
    privy_user_id: Optional[str] = None        # Privy 유저 ID (did:privy:xxx)

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
                               agent_wallet: str) -> dict:
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


# ── 엔드포인트 ────────────────────────────────────────

@router.post("/onboard")
async def onboard_follower(
    body: OnboardRequest,
    background_tasks: BackgroundTasks,
    x_privy_token: Optional[str] = Header(None, alias="X-Privy-Token"),
):
    """
    팔로워 온보딩 전체 플로우:
    1. Solana 주소 형식 검증 (base58, 32-44자)
    2. Privy JWT 선택적 검증 (헤더 있으면 검증)
    3. Builder Code 승인 서명 자동 생성
    4. Pacifica API approve 호출
    5. DB 팔로워 등록 (privy_user_id 포함)
    6. Tier1 트레이더 자동 팔로우 + 모니터링 시작
    """
    from api.main import _db, _engine, _monitors
    from core.position_monitor import RestPositionMonitor
    from db.database import add_follower
    from fuul.referral import FuulReferral

    # ── 입력 검증 ────────────────────────────────────────
    # Step 0a: 팔로워 Solana 주소 검증 (base58 디코딩 + 32바이트 확인)
    _validate_solana_address(body.follower_address, field_name="follower_address")

    # 트레이더 주소 검증 (지정된 경우)
    if body.traders:
        for idx, trader_addr in enumerate(body.traders):
            try:
                _validate_solana_address(str(trader_addr), field_name=f"traders[{idx}]")
            except HTTPException as e:
                raise HTTPException(422, f"traders[{idx}] 주소 오류: {e.detail}")

    # referrer 주소 검증 (지정된 경우)
    if body.referrer_address:
        try:
            _validate_solana_address(body.referrer_address, field_name="referrer_address")
        except HTTPException as e:
            raise HTTPException(422, f"referrer_address 오류: {e.detail}")

    # Step 0b: Privy JWT 선택적 검증
    privy_user_id: Optional[str] = None
    if x_privy_token:
        privy_user_id = _verify_privy_jwt(x_privy_token)
        if privy_user_id:
            logger.info(f"Privy 검증 성공: user_id={privy_user_id}")
        else:
            logger.warning("Privy JWT 검증 실패 — 토큰 무시하고 계속 진행")

    follower = body.follower_address
    traders = body.traders or DEFAULT_TIER1

    result = {
        "follower": follower,
        "builder_code_approved": False,
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
                    copy_ratio=body.copy_ratio,
                    max_position_usdc=body.max_position_usdc
                )
                # builder_code 승인 여부 기록
                if result["builder_code_approved"]:
                    await _db.execute(
                        "UPDATE followers SET builder_code_approved=1 WHERE address=?",
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
        except Exception:
            pass

    result["ok"] = len(result["followers_registered"]) > 0
    result["note"] = (
        f"Builder Code '{BUILDER_CODE}' {'승인됨' if result['builder_code_approved'] else '미승인 (주문은 가능, 수수료 수취 비활성)'}"
    )
    if privy_user_id:
        result["privy_user_id"] = privy_user_id
    return result


@router.get("/list")
async def list_followers(trader_address: Optional[str] = None):
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


@router.delete("/{follower_address}")
async def remove_follower(follower_address: str):
    """팔로워 해지 (soft delete)"""
    from api.main import _db
    if not _db:
        raise HTTPException(503, "DB 미초기화")
    await _db.execute(
        "UPDATE followers SET active=0 WHERE address=?", (follower_address,)
    )
    await _db.commit()
    return {"ok": True, "follower": follower_address, "status": "removed"}
