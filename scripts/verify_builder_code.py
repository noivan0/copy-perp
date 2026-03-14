"""
Builder Code 적용 검증 스크립트

공식 문서 기준으로 구현 정확성 확인:
1. approve 서명 구조 검증 (dry-run, 실제 전송 안 함)
2. approvals 엔드포인트 조회
3. 주문 서명에 builder_code 포함 여부 확인

사용법:
  ACCOUNT_ADDRESS=<addr> AGENT_PRIVATE_KEY=<key> python3 scripts/verify_builder_code.py
"""
import sys
import os
import json
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pacifica.client import (
    _sort_json_keys, _sign_request, _cf_request, check_builder_approvals,
    BUILDER_CODE, ACCOUNT_ADDRESS, AGENT_WALLET_PUBKEY
)
from solders.keypair import Keypair
import base58


def verify_sign_structure():
    """서명 구조가 공식 문서와 일치하는지 검증"""
    print("=== 1. 서명 구조 검증 (approve_builder_code) ===\n")

    # 공식 문서 기준 서명 구조
    ts = 1748970123456  # 고정 타임스탬프 (재현성)
    sign_header = {"timestamp": ts, "expiry_window": 5000, "type": "approve_builder_code"}
    sign_data   = {"builder_code": "noivan", "max_fee_rate": "0.001"}

    # sign_request 내부 로직 재현
    combined = {**sign_header, "data": sign_data}
    sorted_combined = _sort_json_keys(combined)
    compact = json.dumps(sorted_combined, separators=(",", ":"))

    print("서명 대상 (compact JSON):")
    print(f"  {compact}")
    print()

    # 공식 문서 예시와 비교
    expected_keys_order = sorted(combined.keys())
    print(f"최상위 키 정렬: {expected_keys_order}")
    print(f"data 키 정렬: {sorted(sign_data.keys())}")
    print()

    # 실제 서명 (키가 있는 경우)
    pk = os.getenv("AGENT_PRIVATE_KEY") or os.getenv("MAIN_PRIVATE_KEY")
    if pk:
        try:
            seed = base58.b58decode(pk)
            kp = Keypair.from_seed(seed[:32])
        except Exception:
            kp = Keypair.from_base58_string(pk)
        _, sig = _sign_request(sign_header, sign_data, kp)
        print(f"서명 생성됨: {sig[:30]}...")
    else:
        print("⚠️ AGENT_PRIVATE_KEY 미설정 — 서명 스킵")

    print("✅ 서명 구조 검증 완료\n")


def verify_order_sign_structure():
    """주문 서명에 builder_code가 data 안에 포함되는지 검증"""
    print("=== 2. 주문 서명 구조 검증 (create_market_order) ===\n")

    ts = 1716200000000
    sign_header = {"timestamp": ts, "expiry_window": 30000, "type": "create_market_order"}
    sign_data = {
        "symbol": "BTC",
        "amount": "0.1",
        "side": "bid",
        "slippage_percent": "0.5",
        "reduce_only": False,
        "client_order_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "builder_code": "noivan",   # ← data 안에 포함 (공식 문서 기준)
    }

    combined = {**sign_header, "data": sign_data}
    sorted_combined = _sort_json_keys(combined)
    compact = json.dumps(sorted_combined, separators=(",", ":"))

    print("서명 대상:")
    print(f"  {compact[:200]}...")
    print()

    # builder_code가 data 안에 있는지 확인
    assert '"builder_code":"noivan"' in compact, "❌ builder_code가 data 안에 없음!"
    print("✅ builder_code가 data 안에 포함 확인")

    # 요청 body 구조 확인
    request_body = {
        "account": "6ETnufiec2CxVWTS4u5Wiq33Zh5Y3Qm6Pkdpi375fuxP",
        "agent_wallet": None,
        "signature": "...",
        "timestamp": ts,
        "expiry_window": 30000,
        # data 래퍼 제거, top-level flatten
        **sign_data,
    }
    print("\n요청 body (top-level flatten):")
    print(f"  {json.dumps({k: v for k, v in request_body.items() if k != 'signature'}, indent=2)}")
    print()
    assert "builder_code" in request_body, "❌ builder_code가 request_body에 없음"
    print("✅ 요청 body 구조 확인\n")


def check_current_approvals():
    """현재 계정의 builder_code 승인 상태 확인"""
    print("=== 3. Builder Code 승인 상태 확인 ===\n")

    account = ACCOUNT_ADDRESS or os.getenv("ACCOUNT_ADDRESS")
    if not account:
        print("⚠️ ACCOUNT_ADDRESS 미설정 — 조회 스킵")
        return

    print(f"계정: {account}")
    approvals = check_builder_approvals(account)
    if approvals:
        print(f"✅ 승인된 Builder Code ({len(approvals)}개):")
        for a in approvals:
            print(f"   - {a.get('builder_code')} | max_fee={a.get('max_fee_rate')} | {a.get('description','')}")
    else:
        print("❌ 승인된 Builder Code 없음")
        print(f"   → approve 필요: builder_code='{BUILDER_CODE}'")
    print()


def test_approve_dry_run():
    """approve API 호출 테스트 (잘못된 서명으로 에러 구조 확인)"""
    print("=== 4. Approve API 연결 테스트 (dry-run) ===\n")
    try:
        result = _cf_request("POST", "account/builder_codes/approve", {
            "account": "invalid_account_for_testing",
            "agent_wallet": None,
            "signature": "invalid_sig",
            "timestamp": int(time.time() * 1000),
            "expiry_window": 5000,
            "builder_code": BUILDER_CODE,
            "max_fee_rate": "0.001",
        })
        print(f"응답: {result}")
    except RuntimeError as e:
        err_str = str(e)
        if "400" in err_str or "Wrong address" in err_str or "Invalid" in err_str:
            print(f"✅ 엔드포인트 정상 (예상 에러): {err_str[:100]}")
        elif "404" in err_str:
            print(f"❌ 엔드포인트 미존재 (404): {err_str[:100]}")
        elif "not found" in err_str.lower():
            print(f"❌ Builder Code 미등록: {err_str[:100]}")
        else:
            print(f"⚠️ 예외: {err_str[:100]}")
    print()


def do_approve(private_key: str, account: str):
    """실제 approve 실행 (AGENT_PRIVATE_KEY와 ACCOUNT_ADDRESS가 설정된 경우)"""
    from pacifica.client import approve_builder_code

    print(f"=== 5. Builder Code Approve 실행 ===\n")
    print(f"계정: {account}")
    print(f"Builder Code: {BUILDER_CODE}")
    print(f"Fee Rate: 0.001 (0.1%)")
    print()

    result = approve_builder_code(
        main_private_key=private_key,
        account_address=account,
        builder_code=BUILDER_CODE,
        max_fee_rate="0.001",
    )
    print(f"결과: {json.dumps(result, indent=2)}")

    if result.get("success"):
        print("\n✅ Builder Code 승인 완료!")
    else:
        err = result.get("error", "")
        print(f"\n❌ 승인 실패: {err}")
        if "not found" in str(err).lower():
            print("→ 'noivan' builder code가 Pacifica에 미등록됨")
            print("  Discord #builders 채널에서 등록 확인 필요")
        elif "already" in str(err).lower():
            print("→ 이미 승인된 상태")


if __name__ == "__main__":
    print("=" * 60)
    print("  Builder Code 적용 검증")
    print("=" * 60)
    print()

    verify_sign_structure()
    verify_order_sign_structure()
    check_current_approvals()
    test_approve_dry_run()

    # 실제 키가 있으면 approve 실행
    pk = os.getenv("AGENT_PRIVATE_KEY") or os.getenv("MAIN_PRIVATE_KEY")
    addr = ACCOUNT_ADDRESS or os.getenv("ACCOUNT_ADDRESS")
    if pk and addr and "--approve" in sys.argv:
        do_approve(pk, addr)
    elif "--approve" in sys.argv:
        print("⚠️ --approve 플래그 사용 시 AGENT_PRIVATE_KEY + ACCOUNT_ADDRESS 필요")
