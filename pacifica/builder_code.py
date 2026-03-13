"""
Builder Code 연동 — noivan (fee_rate: 0.001 = 0.1%)

[중요] testnet에서 builder code 'noivan'이 Pacifica 서버에 등록되어야 승인 가능.
       현재(2026-03-13) 404 반환 중 → Pacifica Discord/지원팀에 등록 요청 필요.
       
서명 구현 절차 (문서 기준):
1. payload = {timestamp, expiry_window, type, data:{builder_code, max_fee_rate}}
2. 재귀적 키 정렬 (알파벳순)
3. 컴팩트 JSON (공백 없음)
4. UTF-8 인코딩 → Ed25519 서명
5. Base58 인코딩
6. 요청 body = {account, signature, timestamp, expiry_window, type, builder_code, max_fee_rate}
   (data 래퍼 제거, top-level flatten)

플로우:
1. 팔로워가 프론트엔드에서 Privy 지갑으로 로그인
2. approve_builder_code 서명 → 서버 전달
3. 서버가 POST /account/builder_codes/approve 전송
4. 이후 복사 주문마다 builder_code='noivan' 포함
"""

import json
import time
import ssl
import urllib.request
import urllib.error
import os
import logging

from solders.keypair import Keypair
import base58

logger = logging.getLogger(__name__)

# ── CloudFront SNI 우회 설정 ──
CF_URL = os.getenv("PACIFICA_CF_URL", "https://do5jt23sqak4.cloudfront.net")
REST_HOST = os.getenv("PACIFICA_HOST", "test-api.pacifica.fi")
API_KEY = os.getenv("PACIFICA_API_KEY", "")

BUILDER_CODE = os.getenv("BUILDER_CODE", "noivan")
BUILDER_FEE_RATE = os.getenv("BUILDER_FEE_RATE", "0.001")  # 0.1%

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


# ── 핵심 서명 유틸 ──

def _sort_json_keys(obj):
    """재귀적 키 정렬 (알파벳순)"""
    if isinstance(obj, dict):
        return {k: _sort_json_keys(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [_sort_json_keys(i) for i in obj]
    return obj


def create_signature(payload: dict, keypair: Keypair) -> str:
    """
    Pacifica 표준 서명 생성
    1. 재귀 정렬 → 2. 컴팩트 JSON → 3. UTF-8 → 4. Ed25519 → 5. Base58
    """
    sorted_payload = _sort_json_keys(payload)
    compact_json = json.dumps(sorted_payload, separators=(',', ':'), ensure_ascii=False)
    msg_bytes = compact_json.encode('utf-8')
    sig_bytes = keypair.sign_message(msg_bytes)
    return base58.b58encode(bytes(sig_bytes)).decode('ascii')


def build_approve_payload(builder_code: str, max_fee_rate: str) -> dict:
    """서명 대상 페이로드 구성 (approve_builder_code)"""
    return {
        "timestamp": int(time.time() * 1000),
        "expiry_window": 5000,
        "type": "approve_builder_code",
        "data": {
            "builder_code": builder_code,
            "max_fee_rate": max_fee_rate,
        }
    }


# ── HTTP 유틸 ──

def _post_cf(path: str, body: dict) -> tuple[int, dict]:
    """CloudFront SNI 우회 POST"""
    url = f"{CF_URL}{path}"
    data = json.dumps(body).encode('utf-8')
    req = urllib.request.Request(
        url, data=data,
        headers={
            'Host': REST_HOST,
            'Content-Type': 'application/json',
            'X-API-Key': API_KEY,
        },
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body_str = e.read().decode()
        try:
            return e.code, json.loads(body_str)
        except Exception:
            return e.code, {"error": body_str}


def _get_cf(path: str) -> dict:
    """CloudFront SNI 우회 GET"""
    req = urllib.request.Request(
        f"{CF_URL}{path}",
        headers={'Host': REST_HOST, 'X-API-Key': API_KEY}
    )
    with urllib.request.urlopen(req, context=_ssl_ctx, timeout=15) as r:
        return json.loads(r.read())


# ── 핵심 기능 ──

def approve_builder_code(
    account: str,
    keypair: Keypair,
    builder_code: str = BUILDER_CODE,
    max_fee_rate: str = BUILDER_FEE_RATE,
) -> dict:
    """
    팔로워 계정에서 Builder Code 승인
    
    서명 구조:
    - 서명 대상: {timestamp, expiry_window, type, data:{builder_code, max_fee_rate}}
    - 요청 body: data 래퍼 제거 + account + signature top-level
    
    Returns:
        {"ok": True/False, "status": int, "data": ...}
    """
    sign_payload = build_approve_payload(builder_code, max_fee_rate)
    sig = create_signature(sign_payload, keypair)

    # 요청 body: data 래퍼 제거, top-level flatten
    request_body = {
        "timestamp": sign_payload["timestamp"],
        "expiry_window": sign_payload["expiry_window"],
        "type": sign_payload["type"],
        "account": account,
        "signature": sig,
        "builder_code": builder_code,
        "max_fee_rate": max_fee_rate,
    }

    status, resp = _post_cf("/api/v1/account/builder_codes/approve", request_body)

    if status in (200, 201):
        logger.info(f"Builder Code 승인 완료: {account[:12]}... code={builder_code}")
        return {"ok": True, "status": status, "data": resp}
    else:
        err = resp.get("error", str(resp)) if isinstance(resp, dict) else str(resp)
        logger.error(f"Builder Code 승인 실패 HTTP {status}: {err}")
        return {"ok": False, "status": status, "error": err}


def check_approval(account: str, builder_code: str = BUILDER_CODE) -> bool:
    """팔로워가 Builder Code를 승인했는지 확인"""
    try:
        result = _get_cf(f"/api/v1/account/builder_codes/approvals?account={account}")
        approvals = result.get("data", [])
        return any(
            a.get("builder_code") == builder_code
            for a in (approvals or [])
        )
    except Exception as e:
        logger.warning(f"Builder Code 확인 실패: {e}")
        return False


def revoke_builder_code(
    account: str,
    keypair: Keypair,
    builder_code: str = BUILDER_CODE,
) -> dict:
    """Builder Code 승인 취소 (팔로우 해지 시)"""
    sign_payload = {
        "timestamp": int(time.time() * 1000),
        "expiry_window": 5000,
        "type": "revoke_builder_code",
        "data": {"builder_code": builder_code},
    }
    sig = create_signature(sign_payload, keypair)
    request_body = {
        "timestamp": sign_payload["timestamp"],
        "expiry_window": sign_payload["expiry_window"],
        "type": sign_payload["type"],
        "account": account,
        "signature": sig,
        "builder_code": builder_code,
    }
    status, resp = _post_cf("/api/v1/account/builder_codes/revoke", request_body)
    return {"ok": status in (200, 201), "status": status, "data": resp}


# ── 프론트엔드 연동용 (Privy 서명 후 서버 전달) ──

async def handle_frontend_approval(
    account: str,
    signature: str,
    timestamp: int,
    builder_code: str = BUILDER_CODE,
    max_fee_rate: str = BUILDER_FEE_RATE,
) -> dict:
    """
    프론트엔드에서 Privy 지갑으로 서명 완료 후 서버로 전달되는 처리.
    서버는 서명을 그대로 Pacifica API로 포워딩.
    """
    request_body = {
        "timestamp": timestamp,
        "expiry_window": 5000,
        "type": "approve_builder_code",
        "account": account,
        "signature": signature,
        "builder_code": builder_code,
        "max_fee_rate": max_fee_rate,
    }
    status, resp = _post_cf("/api/v1/account/builder_codes/approve", request_body)
    return {"ok": status in (200, 201), "status": status, "data": resp}


# ── CLI 테스트 ──

if __name__ == "__main__":
    import sys
    PRIVATE_KEY = os.getenv("AGENT_PRIVATE_KEY", "")
    if not PRIVATE_KEY:
        print("❌ AGENT_PRIVATE_KEY 미설정")
        sys.exit(1)

    kp = Keypair.from_bytes(base58.b58decode(PRIVATE_KEY))
    account = str(kp.pubkey())
    print(f"계정: {account}")
    print(f"Builder Code: {BUILDER_CODE}")
    print(f"Fee Rate: {BUILDER_FEE_RATE} ({float(BUILDER_FEE_RATE)*100:.2f}%)")

    # 현재 승인 상태 확인
    approved = check_approval(account)
    print(f"\n현재 승인 상태: {'✅ 승인됨' if approved else '❌ 미승인'}")

    if not approved:
        print("\n승인 요청 중...")
        result = approve_builder_code(account, kp)
        print(f"결과: {result}")

        if result["ok"]:
            print("✅ Builder Code 승인 완료!")
        else:
            err = result.get("error", "")
            if "not found" in err.lower():
                print(f"\n⚠️ Builder code '{BUILDER_CODE}'가 Pacifica 서버에 미등록.")
                print("→ Pacifica Discord (#builder-channel)에 등록 요청 필요.")
                print("→ 테스트넷의 경우 @PacificaTGPortalBot에 문의.")
            else:
                print(f"❌ 승인 실패: {err}")


def approve_builder_code(
    account_address: str,
    builder_code: str = BUILDER_CODE,
    max_fee_rate: str = BUILDER_FEE_RATE,
    external_signature: str = None,
    timestamp: int = None,
) -> dict:
    """
    Builder Code 승인 — 프론트 서명 또는 서버 키패어 서명 지원
    
    external_signature: 프론트에서 받은 Base58 서명 (있으면 서버 서명 스킵)
    """
    if external_signature:
        # 프론트에서 이미 서명한 경우
        ts = timestamp or int(time.time() * 1000)
        request_body = {
            "timestamp": ts,
            "expiry_window": 5000,
            "type": "approve_builder_code",
            "account": account_address,
            "signature": external_signature,
            "builder_code": builder_code,
            "max_fee_rate": max_fee_rate,
        }
    else:
        # 서버 키패어로 서명
        from pacifica.client import _load_keypair
        kp = _load_keypair()
        if not kp:
            return {"ok": False, "error": "AGENT_PRIVATE_KEY 미설정"}
        sign_payload = build_approve_payload(builder_code, max_fee_rate)
        sig = create_signature(sign_payload, kp)
        request_body = {
            "timestamp": sign_payload["timestamp"],
            "expiry_window": sign_payload["expiry_window"],
            "type": sign_payload["type"],
            "account": account_address,
            "signature": sig,
            "builder_code": builder_code,
            "max_fee_rate": max_fee_rate,
        }
    
    status, resp = _post_cf("/api/v1/account/builder_codes/approve", request_body)
    if status in (200, 201):
        return {"ok": True, "status": status, "data": resp}
    else:
        err = resp.get("error", str(resp)) if isinstance(resp, dict) else str(resp)
        return {"ok": False, "status": status, "error": err}


def check_builder_approvals(account_address: str) -> list:
    """Builder Code 승인 목록 조회"""
    try:
        result = _get_cf(f"/api/v1/account/builder_codes/approvals?account={account_address}")
        return result.get("data", []) or []
    except Exception as e:
        return []

