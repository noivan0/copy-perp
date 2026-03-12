"""
Fuul 레퍼럴 연동
https://docs.fuul.xyz

Copy Perp에서 트레이더가 팔로워를 초대하면 → Fuul이 보상 자동 분배
"""
import os
import requests
from typing import Optional

FUUL_API_KEY = os.getenv("FUUL_API_KEY", "")
FUUL_PROJECT_ID = os.getenv("FUUL_PROJECT_ID", "")
FUUL_BASE_URL = "https://api.fuul.xyz/v1"


class FuulClient:
    """
    Fuul 레퍼럴 시스템 클라이언트
    - 트레이더가 팔로워 초대 링크 생성
    - 팔로워가 가입 시 트레이더에게 포인트 지급
    """

    def __init__(self):
        self.headers = {
            "Authorization": f"Bearer {FUUL_API_KEY}",
            "Content-Type": "application/json",
        }

    def generate_referral_link(self, trader_address: str) -> str:
        """
        트레이더 레퍼럴 링크 생성
        예: https://copy-perp.app/join?ref=<trader_address>
        """
        base_url = os.getenv("APP_BASE_URL", "https://copy-perp.pacifica.fi")
        return f"{base_url}/join?ref={trader_address}"

    def track_conversion(self, referrer: str, new_user: str, event: str = "signup") -> dict:
        """
        레퍼럴 전환 추적 (팔로워 가입 시 호출)
        API 키 미설정 시 로컬 로깅만 수행
        """
        if not FUUL_API_KEY:
            # API 키 없을 때 → 로컬 기록만
            return {
                "status": "local_only",
                "referrer": referrer,
                "new_user": new_user,
                "event": event,
            }

        try:
            payload = {
                "project_id": FUUL_PROJECT_ID,
                "referrer": referrer,
                "user": new_user,
                "event_name": event,
            }
            r = requests.post(
                f"{FUUL_BASE_URL}/conversions",
                json=payload,
                headers=self.headers,
                timeout=5,
            )
            return r.json()
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def get_referral_stats(self, trader_address: str) -> dict:
        """트레이더 레퍼럴 통계"""
        if not FUUL_API_KEY:
            return {"referrer": trader_address, "total_referrals": 0, "status": "api_key_required"}

        try:
            r = requests.get(
                f"{FUUL_BASE_URL}/stats/{trader_address}",
                headers=self.headers,
                timeout=5,
            )
            return r.json()
        except Exception as e:
            return {"status": "error", "error": str(e)}


# 싱글턴
fuul = FuulClient()
