"""
Fuul 레퍼럴 연동
https://api.fuul.xyz/api/v1/

Fuul은 온체인 레퍼럴/포인트 시스템.
Copy Perp에서:
- 팔로워가 온보딩 시 → 'follow' 이벤트 발송
- 팔로워가 복사 주문 체결 시 → 'copy_trade' 이벤트 발송

현재 상태: FUUL_API_KEY 없으면 Mock 모드
실제 연동 시 .env에 FUUL_API_KEY 설정
"""

import os
import json
import uuid
import time
import logging
import urllib.request
import urllib.error
from typing import Optional

logger = logging.getLogger(__name__)

FUUL_API_BASE = os.getenv("FUUL_API_URL", "https://api.fuul.xyz/api/v1")
FUUL_API_KEY  = os.getenv("FUUL_API_KEY", "")
FUUL_PROJECT_ID = os.getenv("FUUL_PROJECT_ID", "")

# API 키 없으면 Mock 모드
MOCK_MODE = not FUUL_API_KEY.strip()


class FuulReferral:
    """Fuul 레퍼럴 시스템 연동 (SDK 없이 직접 HTTP 호출)"""

    def __init__(self):
        self.mock = MOCK_MODE
        self._points: dict = {}   # in-memory 포인트 (mock/테스트용)
        if self.mock:
            logger.info("Fuul: Mock 모드 (FUUL_API_KEY 미설정 — .env에 추가 후 재시작)")
        else:
            logger.info("Fuul: 실제 연동 모드 (project=%s)", FUUL_PROJECT_ID)

    def _post(self, path: str, body: dict) -> dict:
        """POST /api/v1/{path}"""
        if self.mock:
            return {"ok": True, "mock": True, "event": body.get("name")}

        url = f"{FUUL_API_BASE.rstrip('/')}/{path.lstrip('/')}"
        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={
                "Authorization": f"Bearer {FUUL_API_KEY}",
                "Content-Type": "application/json",
                "X-Fuul-Sdk-Version": "7.17.1",
                "User-Agent": "CopyPerp/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body_err = e.read().decode()
            logger.warning("Fuul API 오류 HTTP %d: %s", e.code, body_err)
            return {"ok": False, "error": body_err, "status": e.code}
        except Exception as e:
            logger.warning("Fuul 연결 실패: %s", e)
            return {"ok": False, "error": str(e)}

    def _make_event(
        self,
        name: str,
        user_address: str,
        args: Optional[dict] = None,
        referrer: Optional[str] = None,
    ) -> dict:
        """표준 Fuul 이벤트 payload 생성"""
        event: dict = {
            "name": name,
            "user": {
                "identifier": user_address,
                "identifier_type": "solana_address",
            },
            "args": args or {},
            "metadata": {
                "tracking_id": str(uuid.uuid4()),
            },
        }
        if FUUL_PROJECT_ID:
            event["metadata"]["project_id"] = FUUL_PROJECT_ID
        if referrer:
            event["metadata"]["referrer"] = referrer
        return event

    # ── Public API ─────────────────────────────────────────────────────────

    def track_follow(
        self,
        follower_address: str,
        trader_address: str,
        referrer: Optional[str] = None,
    ) -> dict:
        """팔로워 온보딩 이벤트"""
        event = self._make_event(
            name="follow",
            user_address=follower_address,
            args={"trader": trader_address, "timestamp": int(time.time())},
            referrer=referrer,
        )
        result = self._post("events", event)
        logger.info("Fuul follow 이벤트: follower=%s trader=%s → %s",
                    follower_address[:8], trader_address[:8], result)
        return result

    def track_copy_trade(
        self,
        follower_address: str,
        trader_address: str,
        symbol: str,
        side: str,
        amount_usdc: float,
        order_id: Optional[str] = None,
    ) -> dict:
        """복사 주문 체결 이벤트"""
        event = self._make_event(
            name="copy_trade",
            user_address=follower_address,
            args={
                "trader": trader_address,
                "symbol": symbol,
                "side": side,
                "amount_usdc": amount_usdc,
                "order_id": order_id or "",
                "timestamp": int(time.time()),
            },
        )
        result = self._post("events", event)
        logger.info("Fuul copy_trade: follower=%s %s %s $%.2f → %s",
                    follower_address[:8], symbol, side, amount_usdc, result)
        return result

    def track_pageview(self, page: str = "/") -> dict:
        """페이지뷰 이벤트 (브라우저 컨텍스트 없으므로 서버 측 호출)"""
        event = {
            "name": "pageview",
            "args": {"page": page, "locationOrigin": "https://copy-perp.vercel.app"},
            "metadata": {"tracking_id": str(uuid.uuid4())},
        }
        if FUUL_PROJECT_ID:
            event["metadata"]["project_id"] = FUUL_PROJECT_ID
        return self._post("events", event)

    def identify_user(self, address: str) -> dict:
        """지갑 연결 이벤트 (connect_wallet)"""
        event = self._make_event(
            name="connect_wallet",
            user_address=address,
            args={"page": "/", "locationOrigin": "https://copy-perp.vercel.app"},
        )
        return self._post("events", event)

    # ── 테스트/프론트 호환 메서드 ──────────────────────────────────────────

    def generate_referral_link(self, address: str) -> str:
        """레퍼럴 링크 생성 (ref= 코드 포함)"""
        short = address[:8]
        return f"https://copy-perp.vercel.app/?ref={short}"

    async def track_referral(self, referrer: str, referee: str) -> dict:
        """레퍼럴 추적 (async alias for track_follow)"""
        result = self.track_follow(
            follower_address=referee,
            trader_address=referrer,
            referrer=referrer,
        )
        # in-memory 포인트 누적 (mock + 실제 모두)
        self._points[referrer] = self._points.get(referrer, 0) + 10
        return result

    async def track_trade_volume(self, address: str, volume_usdc: float) -> dict:
        """거래 볼륨 기반 포인트 적립 (0.1% = 0.001 per USDC)"""
        pts = volume_usdc * 0.001
        self._points[address] = self._points.get(address, 0) + pts
        event = self._make_event(
            name="trade_volume",
            user_address=address,
            args={"volume_usdc": volume_usdc, "points_earned": pts},
        )
        result = self._post("events", event)
        result["ok"] = True
        result["points_earned"] = pts
        return result

    def get_points(self, address: str) -> float:
        """레퍼럴 포인트 조회 (in-memory, mock용)"""
        return self._points.get(address, 0)

    def get_leaderboard(self, limit: int = 10) -> list:
        """포인트 리더보드"""
        sorted_pts = sorted(self._points.items(), key=lambda x: x[1], reverse=True)
        return [{"address": addr, "points": pts} for addr, pts in sorted_pts[:limit]]

    def get_referral_stats(self, address: str) -> dict:
        """레퍼럴 현황 조회 (Mock에서는 더미 반환)"""
        if self.mock:
            return {
                "address": address,
                "referrals": 0,
                "total_volume": 0.0,
                "mock": True,
            }
        try:
            url = f"{FUUL_API_BASE.rstrip('/')}/payouts/leaderboard/referred-volume?user_identifiers={address}"
            req = urllib.request.Request(
                url,
                headers={"Authorization": f"Bearer {FUUL_API_KEY}", "User-Agent": "CopyPerp/1.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except Exception as e:
            return {"ok": False, "error": str(e)}


# ── 싱글턴 ─────────────────────────────────────────────────────────────────
_fuul = None


def get_fuul() -> FuulReferral:
    global _fuul
    if _fuul is None:
        _fuul = FuulReferral()
    return _fuul
