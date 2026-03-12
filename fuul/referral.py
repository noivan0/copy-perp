"""
Fuul 레퍼럴 연동
https://www.fuul.xyz/

Fuul은 온체인 레퍼럴/포인트 시스템.
Copy Perp에서:
- 팔로워가 다른 사람을 초대 → 레퍼럴 포인트 적립
- 트레이더가 팔로워를 유치 → 추가 보상

현재 상태: Mock 구현 (Fuul API 키 미수령)
실제 연동 시 FUUL_API_KEY 환경변수 설정 필요
"""

import os
import json
import time
import uuid
import logging
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

FUUL_API_URL = os.getenv("FUUL_API_URL", "https://api.fuul.xyz")
FUUL_API_KEY = os.getenv("FUUL_API_KEY", "")
FUUL_PROJECT_ID = os.getenv("FUUL_PROJECT_ID", "")

# Mock 모드 (API 키 없을 때)
MOCK_MODE = not FUUL_API_KEY


class FuulReferral:
    """Fuul 레퍼럴 시스템 연동"""

    def __init__(self):
        self.mock = MOCK_MODE
        if self.mock:
            logger.info("Fuul: Mock 모드 (FUUL_API_KEY 미설정)")
        else:
            logger.info(f"Fuul: 실제 연동 (project={FUUL_PROJECT_ID})")

        # Mock 저장소
        self._referrals: dict[str, str] = {}   # referee → referrer
        self._points: dict[str, float] = {}     # address → points

    def generate_referral_link(self, address: str) -> str:
        """팔로워/트레이더 레퍼럴 링크 생성"""
        # ref 코드 = 주소 앞 8자
        ref_code = address[:8]
        base_url = os.getenv("APP_URL", "https://copy-perp.pacifica.fi")
        return f"{base_url}?ref={ref_code}"

    async def track_referral(self, referrer: str, referee: str) -> dict:
        """레퍼럴 추적 (신규 팔로워 등록 시 호출)"""
        if self.mock:
            self._referrals[referee] = referrer
            # 레퍼러에게 포인트 적립 (Mock)
            self._points[referrer] = self._points.get(referrer, 0) + 10.0
            logger.info(f"[Mock] 레퍼럴: {referee[:8]} ← {referrer[:8]} (+10pt)")
            return {"ok": True, "mock": True, "points_awarded": 10.0}

        # 실제 Fuul API 호출
        try:
            body = {
                "project_id": FUUL_PROJECT_ID,
                "referrer": referrer,
                "referee": referee,
                "timestamp": int(time.time()),
            }
            req = urllib.request.Request(
                f"{FUUL_API_URL}/v1/referrals",
                data=json.dumps(body).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {FUUL_API_KEY}",
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read())
        except Exception as e:
            logger.error(f"Fuul API 오류: {e}")
            return {"ok": False, "error": str(e)}

    async def track_trade_volume(self, address: str, volume_usdc: float) -> dict:
        """거래 볼륨 기반 포인트 적립"""
        if self.mock:
            pts = volume_usdc * 0.001  # 1 USDC당 0.001 포인트
            self._points[address] = self._points.get(address, 0) + pts
            return {"ok": True, "mock": True, "points": pts}

        try:
            body = {
                "project_id": FUUL_PROJECT_ID,
                "address": address,
                "volume_usd": volume_usdc,
                "event_type": "trade",
            }
            req = urllib.request.Request(
                f"{FUUL_API_URL}/v1/events",
                data=json.dumps(body).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {FUUL_API_KEY}",
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read())
        except Exception as e:
            logger.error(f"Fuul 볼륨 추적 오류: {e}")
            return {"ok": False, "error": str(e)}

    def get_points(self, address: str) -> float:
        """포인트 조회 (Mock)"""
        return self._points.get(address, 0.0)

    def get_leaderboard(self, limit: int = 10) -> list:
        """포인트 리더보드 (Mock)"""
        sorted_pts = sorted(self._points.items(), key=lambda x: x[1], reverse=True)
        return [{"address": addr, "points": pts} for addr, pts in sorted_pts[:limit]]


# 싱글턴
fuul = FuulReferral()


if __name__ == "__main__":
    import asyncio

    async def test():
        f = FuulReferral()
        link = f.generate_referral_link("3AHZqrocSguMuo9sUUP8G8YN8NwHwWV2DPUQvbDvtfaQ")
        print(f"레퍼럴 링크: {link}")

        r = await f.track_referral("TraderAAA...", "FollowerBBB...")
        print(f"레퍼럴 추적: {r}")

        r2 = await f.track_trade_volume("TraderAAA...", 500.0)
        print(f"볼륨 적립: {r2}")

        print(f"TraderAAA 포인트: {f.get_points('TraderAAA...')}")
        print(f"리더보드: {f.get_leaderboard()}")

    asyncio.run(test())
