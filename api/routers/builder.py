"""
Builder Code 라우터
POST /builder/approve    — 팔로워가 프론트(Privy)에서 서명한 승인 서버로 전달
GET  /builder/check      — 팔로워의 builder code 승인 여부 확인
POST /builder/revoke     — 팔로워 builder code 승인 취소
GET  /builder/stats      — 빌더 수익 통계 (노이반 지갑 기준)

플로우:
  1. 프론트엔드 → Privy 지갑으로 approve_builder_code 서명
  2. POST /builder/approve 로 {account, signature, timestamp, max_fee_rate} 전달
  3. 서버 → Pacifica POST /account/builder_codes/approve 전달
  4. 이후 모든 복사 주문에 builder_code="noivan" 자동 포함
"""

import os
import json
import time
import ssl
import urllib.request
import urllib.error
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/builder", tags=["builder"])

REST_URL = os.getenv("PACIFICA_REST_URL", "https://test-api.pacifica.fi/api/v1")
BUILDER_CODE = os.getenv("BUILDER_CODE", "noivan")
BUILDER_FEE_RATE = os.getenv("BUILDER_FEE_RATE", "0.0005")
ACCOUNT_ADDRESS = os.getenv("ACCOUNT_ADDRESS", "")

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def _post(path: str, body: dict) -> dict:
    """CloudFront SNI 스푸핑으로 HMG 웹필터 우회 POST"""
    from pacifica.client import _cf_request
    return _cf_request("POST", path, body)


def _get(path: str) -> dict:
    """CloudFront SNI 스푸핑으로 HMG 웹필터 우회 GET"""
    from pacifica.client import _cf_request
    return _cf_request("GET", path)


# ── 요청 모델 ─────────────────────────────────────────

class ApproveBuilderCodeRequest(BaseModel):
    """프론트엔드(Privy)에서 서명 완료 후 전달하는 페이로드"""
    account: str
    signature: str
    timestamp: int
    max_fee_rate: str = BUILDER_FEE_RATE
    agent_wallet: Optional[str] = None

class RevokeBuilderCodeRequest(BaseModel):
    account: str
    signature: str
    timestamp: int
    agent_wallet: Optional[str] = None

class PrepareApprovalRequest(BaseModel):
    """프론트엔드가 서명할 메시지 페이로드 생성 요청"""
    account: str
    max_fee_rate: str = BUILDER_FEE_RATE


# ── 엔드포인트 ─────────────────────────────────────────

@router.post("/prepare-approval")
def prepare_approval(body: PrepareApprovalRequest):
    """
    프론트엔드(Privy)가 서명할 메시지 구조 반환.
    클라이언트는 이 payload를 Privy signMessage로 서명 후 /builder/approve로 전송.
    """
    timestamp = int(time.time() * 1000)
    payload_to_sign = {
        "timestamp": timestamp,
        "expiry_window": 5000,
        "type": "approve_builder_code",
        "data": {
            "builder_code": BUILDER_CODE,
            "max_fee_rate": body.max_fee_rate,
        },
    }
    # JSON 키 알파벳 정렬 (Pacifica 서명 규칙)
    def sort_keys(v):
        if isinstance(v, dict):
            return {k: sort_keys(v[k]) for k in sorted(v.keys())}
        if isinstance(v, list):
            return [sort_keys(i) for i in v]
        return v

    sorted_payload = sort_keys(payload_to_sign)
    message_string = json.dumps(sorted_payload, separators=(",", ":"))

    return {
        "builder_code": BUILDER_CODE,
        "max_fee_rate": body.max_fee_rate,
        "timestamp": timestamp,
        "message": message_string,       # Privy signMessage에 넣을 문자열
        "payload": sorted_payload,       # 참고용
    }


@router.post("/approve")
async def approve_builder_code(body: ApproveBuilderCodeRequest):
    """
    팔로워가 Privy로 서명한 builder code 승인을 Pacifica API로 전달.
    성공 시 이후 모든 복사 주문에 builder_code="noivan" 자동 포함.
    """
    pacifica_body = {
        "account": body.account,
        "agent_wallet": body.agent_wallet,
        "signature": body.signature,
        "timestamp": body.timestamp,
        "expiry_window": 5000,
        "builder_code": BUILDER_CODE,
        "max_fee_rate": body.max_fee_rate,
    }

    try:
        result = _post("account/builder_codes/approve", pacifica_body)
        logger.info(f"Builder Code 승인: {body.account[:12]}... → {result}")

        # DB에 승인 상태 기록
        from api.main import _db
        if _db:
            await _db.execute(
                "UPDATE followers SET builder_code_approved=1 WHERE address=?",
                (body.account,)
            )
            await _db.commit()

        return {"ok": True, "builder_code": BUILDER_CODE, "result": result}
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@router.get("/check")
def check_approval(account: str):
    """팔로워의 builder code 승인 여부 확인"""
    try:
        result = _get(f"account/builder_codes/approvals?account={account}")
        approvals = result if isinstance(result, list) else result.get("data", [])
        approved = any(
            a.get("builder_code") == BUILDER_CODE for a in approvals
        )
        matching = next(
            (a for a in approvals if a.get("builder_code") == BUILDER_CODE), None
        )
        return {
            "account": account,
            "builder_code": BUILDER_CODE,
            "approved": approved,
            "max_fee_rate": matching.get("max_fee_rate") if matching else None,
            "approved_at": matching.get("updated_at") if matching else None,
        }
    except RuntimeError as e:
        raise HTTPException(500, str(e))


@router.post("/revoke")
async def revoke_builder_code(body: RevokeBuilderCodeRequest):
    """팔로우 해지 시 builder code 승인 취소"""
    pacifica_body = {
        "account": body.account,
        "agent_wallet": body.agent_wallet,
        "signature": body.signature,
        "timestamp": body.timestamp,
        "expiry_window": 5000,
        "builder_code": BUILDER_CODE,
    }
    try:
        result = _post("account/builder_codes/revoke", pacifica_body)

        from api.main import _db
        if _db:
            await _db.execute(
                "UPDATE followers SET builder_code_approved=0 WHERE address=?",
                (body.account,)
            )
            await _db.commit()

        return {"ok": True, "builder_code": BUILDER_CODE, "result": result}
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@router.get("/stats")
def get_builder_stats():
    """빌더 수익 통계 (builder_code=noivan)"""
    try:
        result = _get(f"builder/overview?account={ACCOUNT_ADDRESS}")
        data = result if isinstance(result, dict) and "data" not in result else result.get("data", result)
        return {"ok": True, "builder_code": BUILDER_CODE, "data": data}
    except RuntimeError as e:
        logger.warning(f"Builder stats 조회 실패: {e}")
        return {"ok": False, "error": str(e), "builder_code": BUILDER_CODE}


@router.get("/trades")
def get_builder_trades(limit: int = 50):
    """빌더 코드로 발생한 거래 내역"""
    try:
        result = _get(f"builder/trades?builder_code={BUILDER_CODE}&limit={limit}")
        data = result if isinstance(result, list) else result.get("data", [])
        return {"ok": True, "trades": data, "count": len(data)}
    except RuntimeError as e:
        return {"ok": False, "error": str(e), "trades": []}


@router.get("/leaderboard")
def get_builder_leaderboard(limit: int = 20):
    """빌더 코드 유저 리더보드"""
    try:
        result = _get(f"leaderboard/builder_code?builder_code={BUILDER_CODE}&limit={limit}")
        data = result if isinstance(result, list) else result.get("data", [])
        return {"ok": True, "leaderboard": data}
    except RuntimeError as e:
        return {"ok": False, "error": str(e), "leaderboard": []}
