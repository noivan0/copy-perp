"""
Builder Code 연동
- 코드: 'copyperp' (영숫자 3-16자 제약)
- 팔로워 등록 시 approve_builder_code 서명 필요
- 이후 모든 복사 주문에 builder_code 파라미터 포함

플로우:
1. 팔로워가 프론트엔드에서 Privy 지갑으로 로그인
2. approve_builder_code 서명 → 서버 전달
3. 서버가 POST /account/builder_codes/approve 전송
4. 이후 복사 주문마다 builder_code='copyperp' 포함
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

REST_URL = os.getenv("PACIFICA_REST_URL", "https://api.pacifica.fi/api/v1")
BUILDER_CODE = os.getenv("BUILDER_CODE", "noivan")
BUILDER_FEE_RATE = os.getenv("BUILDER_FEE_RATE", "0.0005")  # 0.05%

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def _sort_json_keys(value):
    if isinstance(value, dict):
        return {k: _sort_json_keys(v) for k in sorted(value.keys())}
    elif isinstance(value, list):
        return [_sort_json_keys(i) for i in value]
    return value


def _post(path: str, body: dict) -> dict:
    url = f"{REST_URL}/{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "User-Agent": "CopyPerp/1.0"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()}")


def sign_and_post(endpoint_type: str, endpoint_path: str, payload: dict,
                  keypair: Keypair, account: str, agent_pub: str = None) -> dict:
    """SDK 서명 방식으로 POST"""
    timestamp = int(time.time() * 1000)
    header = {"timestamp": timestamp, "expiry_window": 5000, "type": endpoint_type}
    data = {**header, "data": payload}
    message = json.dumps(_sort_json_keys(data), separators=(",", ":"))
    sig_bytes = keypair.sign_message(message.encode("utf-8"))
    signature = base58.b58encode(bytes(sig_bytes)).decode("ascii")

    body = {
        "account": account,
        "agent_wallet": agent_pub,
        "signature": signature,
        "timestamp": timestamp,
        "expiry_window": 5000,
        **payload,
    }
    return _post(endpoint_path, body)


def approve_builder_code(
    account: str,
    keypair: Keypair,
    builder_code: str = BUILDER_CODE,
    max_fee_rate: str = BUILDER_FEE_RATE,
) -> dict:
    """
    팔로워 계정에서 Builder Code 승인
    - 팔로워 자신의 keypair로 서명
    - 실제 운영: 프론트엔드에서 Privy 지갑으로 서명 후 서버로 전달
    """
    payload = {"builder_code": builder_code, "max_fee_rate": max_fee_rate}
    result = sign_and_post(
        "approve_builder_code",
        "account/builder_codes/approve",
        payload, keypair, account
    )
    logger.info(f"Builder Code 승인: {account[:12]}... → {result}")
    return result


def check_approval(account: str, builder_code: str = BUILDER_CODE) -> bool:
    """팔로워가 Builder Code를 승인했는지 확인"""
    url = f"{REST_URL}/account/builder_codes/approvals?account={account}"
    req = urllib.request.Request(url, headers={"User-Agent": "CopyPerp/1.0"})
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=8) as r:
            result = json.loads(r.read())
            approvals = result.get("data", [])
            return any(a.get("builder_code") == builder_code for a in approvals)
    except Exception as e:
        logger.warning(f"Builder Code 확인 실패: {e}")
        return False


def revoke_builder_code(
    account: str,
    keypair: Keypair,
    builder_code: str = BUILDER_CODE,
) -> dict:
    """Builder Code 승인 취소 (팔로우 해지 시)"""
    payload = {"builder_code": builder_code}
    return sign_and_post(
        "revoke_builder_code",
        "account/builder_codes/revoke",
        payload, keypair, account
    )


# FastAPI 연동용 — 서버가 팔로워 대신 Builder Code 승인 처리
# 실제 운영: 프론트엔드에서 Privy로 서명 → 이 엔드포인트로 POST
async def handle_follower_approval_from_frontend(
    account: str,
    signature: str,
    timestamp: int,
    builder_code: str = BUILDER_CODE,
    max_fee_rate: str = BUILDER_FEE_RATE,
) -> dict:
    """
    프론트엔드에서 Privy 서명 완료 후 서버로 전달되는 처리
    서버는 서명을 그대로 Pacifica API로 전달
    """
    body = {
        "account": account,
        "agent_wallet": None,
        "signature": signature,
        "timestamp": timestamp,
        "expiry_window": 5000,
        "builder_code": builder_code,
        "max_fee_rate": max_fee_rate,
    }
    return _post("account/builder_codes/approve", body)


if __name__ == "__main__":
    print(f"Builder Code: {BUILDER_CODE}")
    print(f"Fee Rate: {BUILDER_FEE_RATE} ({float(BUILDER_FEE_RATE)*100:.3f}%)")

    # 주문 페이로드 예시
    example = {
        "symbol": "BTC",
        "side": "bid",
        "amount": "0.01",
        "builder_code": BUILDER_CODE,
    }
    print(f"\n주문 페이로드 예시:\n{json.dumps(example, indent=2)}")
