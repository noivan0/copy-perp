"""
Builder Code API 라우터
========================
POST /builder/prepare-approval  — 프론트가 서명할 메시지 생성
POST /builder/approve           — 팔로워 builder code 승인 (Privy 서명 or 서버 서명)
GET  /builder/check             — 팔로워 승인 여부 확인
POST /builder/revoke            — 승인 취소
GET  /builder/stats             — 빌더 수익 통계
GET  /builder/trades            — 빌더 코드 거래 내역

## 팔로워 온보딩 플로우 (프론트엔드)
1. GET /builder/prepare-approval?account=<addr>
   → { message, timestamp, builder_code, max_fee_rate }

2. Privy.signMessage(message) → signature (Base58)

3. POST /builder/approve
   → { account, signature, timestamp }
   → Pacifica API로 포워딩 → DB builder_code_approved = 1 업데이트

4. 이후 모든 복사 주문에 builder_code="noivan" 자동 포함
"""

import os
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from pacifica.builder_code import (
    BUILDER_CODE,
    BUILDER_FEE_RATE,
    approve,
    revoke,
    check_approval,
    get_builder_trades,
    get_builder_revenue,
    prepare_approval_message,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/builder", tags=["builder"])


# ── 요청 모델 ─────────────────────────────────────────────

class PrepareApprovalReq(BaseModel):
    account: str
    builder_code: str = BUILDER_CODE
    max_fee_rate: str = BUILDER_FEE_RATE


class ApproveReq(BaseModel):
    account:      str
    signature:    str            # Privy signMessage 결과 (Base58)
    timestamp:    int            # prepare-approval에서 받은 timestamp
    agent_wallet: Optional[str] = None
    builder_code: str = BUILDER_CODE
    max_fee_rate: str = BUILDER_FEE_RATE


class RevokeReq(BaseModel):
    account:      str
    signature:    str
    timestamp:    int
    agent_wallet: Optional[str] = None
    builder_code: str = BUILDER_CODE


# ── 엔드포인트 ────────────────────────────────────────────

@router.get("/prepare-approval")
def prepare_approval(account: str):
    """
    프론트엔드가 Privy.signMessage()에 넣을 메시지 생성.

    Response:
        message:      compact JSON 문자열 (signMessage 입력)
        timestamp:    ms (approve 요청 시 그대로 사용)
        builder_code: "noivan"
        max_fee_rate: BUILDER_FEE_RATE
    """
    return prepare_approval_message(account)


@router.post("/approve")
async def approve_builder_code(body: ApproveReq, request: Request):
    """
    팔로워가 Privy로 서명한 builder code 승인을 Pacifica에 전달.
    성공 시 DB builder_code_approved = 1 업데이트.
    Rate limit: IP당 분당 3회
    """
    from api.utils import get_client_ip as _gcip, check_rate_limit as _crl
    client_ip = _gcip(request)
    if not _crl(f"builder_approve:{client_ip}", 3, 60):
        raise HTTPException(429, {"error": "Rate limit exceeded", "code": "RATE_LIMIT_EXCEEDED"})

    result = approve(
        account      = body.account,
        signature    = body.signature,
        timestamp    = body.timestamp,
        builder_code = body.builder_code,
        max_fee_rate = body.max_fee_rate,
        agent_wallet = body.agent_wallet,
    )

    if result["ok"]:
        # DB 업데이트
        try:
            from api.deps import _get_db_direct
            _db = _get_db_direct()
            if _db:
                await _db.execute(
                    "UPDATE followers SET builder_code_approved=1, builder_approved=1 WHERE address=?",
                    (body.account,),
                )
                await _db.commit()
                logger.info(f"DB 업데이트: {body.account[:16]}... builder_code_approved=1")
        except Exception as e:
            logger.warning(f"DB 업데이트 실패 (승인은 성공): {e}")

    if not result["ok"]:
        status = result.get("status", 400)
        raise HTTPException(
            status_code=status if status >= 400 else 400,
            detail=result.get("response", {})
        )

    return {
        "ok":           True,
        "builder_code": body.builder_code,
        "account":      body.account,
        "response":     result.get("response"),
    }


@router.get("/check")
def check(account: str, builder_code: str = BUILDER_CODE):
    """팔로워의 builder code 승인 여부 확인"""
    approved = check_approval(account, builder_code)
    return {
        "account":      account,
        "builder_code": builder_code,
        "approved":     approved,
    }


@router.post("/revoke")
async def revoke_builder_code(body: RevokeReq):
    """Builder Code 승인 취소"""
    result = revoke(
        account      = body.account,
        signature    = body.signature,
        timestamp    = body.timestamp,
        builder_code = body.builder_code,
        agent_wallet = body.agent_wallet,
    )

    if result["ok"]:
        try:
            from api.deps import _get_db_direct
            _db = _get_db_direct()
            if _db:
                await _db.execute(
                    "UPDATE followers SET builder_code_approved=0, builder_approved=0 WHERE address=?",
                    (body.account,),
                )
                await _db.commit()
        except Exception as e:
            logger.warning(f"DB revoke 업데이트 실패: {e}")

    return result


_builder_stats_cache: dict = {"data": None, "ts": 0.0}
_BUILDER_STATS_TTL = 60.0  # 60초 캐시

@router.get("/stats")
def builder_stats():
    """noivan 빌더 코드 수익 통계 (60초 캐시)"""
    import time
    now = time.time()
    if _builder_stats_cache["data"] and now - _builder_stats_cache["ts"] < _BUILDER_STATS_TTL:
        return {**_builder_stats_cache["data"], "cached": True}
    data = get_builder_revenue(BUILDER_CODE)
    _builder_stats_cache["data"] = data
    _builder_stats_cache["ts"] = now
    return {**data, "cached": False}


@router.get("/trades")
def builder_trades(limit: int = 100):
    """noivan 빌더 코드로 발생한 거래 내역"""
    trades = get_builder_trades(BUILDER_CODE, limit)
    return {
        "builder_code": BUILDER_CODE,
        "count":        len(trades),
        "trades":       trades,
    }
