"""
Builder Code 구현
- Builder Code = 우리가 직접 정의하는 문자열 (신청 불필요)
- 팔로워가 우리 플랫폼 통해 주문 시 → 유저가 먼저 승인 서명
- 주문마다 builder_code 파라미터 포함 → 수수료 자동 수취

플로우:
1. 팔로워가 Copy Perp에 등록 시 → approve_builder_code 서명 요청
2. 팔로워 승인 후 → 모든 복사 주문에 builder_code 포함
"""

import json
import time
import ssl
import urllib.request
import urllib.error
import os

from solders.keypair import Keypair
import base58

from pacifica.client import _ssl_ctx, REST_URL

# 우리 Builder Code (임의 문자열, 팔로워가 approve해야 함)
BUILDER_CODE = os.getenv("BUILDER_CODE", "copyperp")
# 우리가 추가로 가져갈 수수료 비율 (0.001 = 0.1%)
BUILDER_FEE_RATE = os.getenv("BUILDER_FEE_RATE", "0.0005")  # 0.05%


def _request_post(path: str, body: dict) -> dict:
    url = f"{REST_URL}/{path}"
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "CopyPerp/1.0"}
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()}")


def build_approve_payload(
    follower_keypair: Keypair,
    builder_code: str = BUILDER_CODE,
    max_fee_rate: str = BUILDER_FEE_RATE,
) -> dict:
    """
    팔로워가 Builder Code를 승인하는 서명 페이로드 생성
    팔로워 지갑 키로 서명 필요 (Privy 지갑이면 프론트엔드에서 처리)
    """
    from pacifica.client import _sign_request

    timestamp = int(time.time() * 1000)

    payload = {
        "timestamp": timestamp,
        "expiry_window": 5000,
        "type": "approve_builder_code",
        "data": {
            "builder_code": builder_code,
            "max_fee_rate": max_fee_rate,
        }
    }

    sig = _sign_request(payload, follower_keypair)

    return {
        "account": str(follower_keypair.pubkey()),
        "signature": sig,
        "timestamp": timestamp,
        "expiry_window": 5000,
        "data": {
            "builder_code": builder_code,
            "max_fee_rate": max_fee_rate,
        }
    }


def approve_builder_code(follower_keypair: Keypair) -> dict:
    """
    팔로워가 Builder Code 승인
    → POST /api/v1/account/builder_codes/approve
    
    실제 운영: Privy 지갑을 쓰는 팔로워는 프론트에서 서명 처리
    테스트: 개발팀 keypair로 직접 테스트
    """
    payload = build_approve_payload(follower_keypair)
    return _request_post("account/builder_codes/approve", payload)


def check_approvals(account: str) -> dict:
    """
    팔로워가 승인한 Builder Code 목록 조회
    GET /api/v1/account/builder_codes/approvals?account=xxx
    """
    url = f"{REST_URL}/account/builder_codes/approvals?account={account}"
    req = urllib.request.Request(url, headers={"User-Agent": "CopyPerp/1.0"})
    with urllib.request.urlopen(req, context=_ssl_ctx, timeout=10) as r:
        return json.loads(r.read())


def is_approved(account: str, builder_code: str = BUILDER_CODE) -> bool:
    """팔로워가 해당 Builder Code를 승인했는지 확인"""
    try:
        result = check_approvals(account)
        approvals = result.get("data", [])
        return any(a.get("builder_code") == builder_code for a in approvals)
    except Exception:
        return False


if __name__ == "__main__":
    print(f"Builder Code: {BUILDER_CODE}")
    print(f"Fee Rate: {BUILDER_FEE_RATE} ({float(BUILDER_FEE_RATE)*100:.3f}%)")
    print()
    print("주문 시 포함 방식:")
    print(json.dumps({
        "symbol": "BTC",
        "side": "bid",
        "amount": "100",
        "builder_code": BUILDER_CODE,  # 이걸 주문 페이로드에 포함
    }, indent=2))
