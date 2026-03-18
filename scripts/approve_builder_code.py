"""
scripts/approve_builder_code.py
Builder Code 'noivan' approve 실행 스크립트

실행 방법:
  python3 scripts/approve_builder_code.py <MAIN_PRIVATE_KEY>

  또는 환경변수로:
  MAIN_PRIVATE_KEY=<key> python3 scripts/approve_builder_code.py

주의:
  - MAIN_PRIVATE_KEY는 account 주인(3AHZqroc...)의 private key여야 합니다.
  - Agent wallet key(9mxJJAQw...)로는 승인 불가.
  - 서명 구조: sort_json_keys({timestamp, expiry_window, type, data}) → compact → sign → base58
"""

import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from dotenv import load_dotenv
load_dotenv()

from pacifica.client import approve_builder_code, check_builder_approvals
from pacifica.builder_code import BUILDER_FEE_RATE

ACCOUNT = os.getenv("ACCOUNT_ADDRESS", "3AHZqrocSguMuo9sUUP8G8YN8NwHwWV2DPUQvbDvtfaQ")
BUILDER_CODE = os.getenv("BUILDER_CODE", "noivan")
MAX_FEE_RATE = BUILDER_FEE_RATE


def main():
    # main private key 획득
    main_key = None
    if len(sys.argv) > 1:
        main_key = sys.argv[1]
    else:
        main_key = os.getenv("MAIN_PRIVATE_KEY")

    if not main_key:
        print("❌ 사용법: python3 scripts/approve_builder_code.py <MAIN_PRIVATE_KEY>")
        print("   또는:   MAIN_PRIVATE_KEY=<key> python3 scripts/approve_builder_code.py")
        print()
        print("⚠️  MAIN_PRIVATE_KEY는 account 주인의 private key입니다.")
        print(f"   Account: {ACCOUNT}")
        sys.exit(1)

    print("=" * 60)
    print(f"Builder Code Approve")
    print("=" * 60)
    print(f"Account:       {ACCOUNT}")
    print(f"Builder Code:  {BUILDER_CODE}")
    print(f"Max Fee Rate:  {MAX_FEE_RATE}")
    print()

    # 현재 승인 상태 먼저 확인
    print("현재 승인 상태 확인...")
    try:
        current = check_builder_approvals(ACCOUNT)
        if current:
            print(f"  이미 승인된 코드: {current}")
            for c in current:
                if isinstance(c, dict) and c.get("builder_code") == BUILDER_CODE:
                    print(f"  ✅ '{BUILDER_CODE}' 이미 승인됨!")
                    return
        else:
            print("  승인된 코드 없음")
    except Exception as e:
        print(f"  조회 실패: {e}")

    print()
    print("approve 요청 전송...")
    try:
        result = approve_builder_code(
            main_private_key=main_key,
            account_address=ACCOUNT,
            builder_code=BUILDER_CODE,
            max_fee_rate=MAX_FEE_RATE,
        )
        print(f"응답: {json.dumps(result, indent=2)}")
        if result.get("success"):
            print(f"\n✅ Builder Code '{BUILDER_CODE}' 승인 완료!")
        else:
            print(f"\n❌ 실패: {result.get('error')}")
    except Exception as e:
        print(f"❌ 오류: {e}")

    print()
    print("승인 후 상태 재확인...")
    try:
        after = check_builder_approvals(ACCOUNT)
        print(f"  현재 승인 목록: {after}")
    except Exception as e:
        print(f"  조회 실패: {e}")


if __name__ == "__main__":
    main()
