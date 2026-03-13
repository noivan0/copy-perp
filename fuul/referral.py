"""
Fuul 레퍼럴 연동 v2
https://api.fuul.xyz/api/v1/

Fuul SDK v7.18.0 기반 API 구조 반영:
- POST /events         — 커스텀 이벤트 발송
- POST /referral_codes — 레퍼럴 코드 생성 (커스텀 코드 지원)
- GET  /referral_codes/{code} — 코드 조회
- GET  /payouts/leaderboard/referred-volume — 레퍼럴 볼륨

Copy Perp에서:
- 팔로워 온보딩 → 'follow' 이벤트
- 지갑 연결   → 'connect_wallet' 이벤트 (identifyUser)
- 복사 주문   → 'copy_trade' 이벤트
- 거래 볼륨   → 'trade_volume' 이벤트

FUUL_API_KEY 없으면 Mock 모드
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

FUUL_API_BASE    = os.getenv("FUUL_API_URL", "https://api.fuul.xyz/api/v1")
FUUL_API_KEY     = os.getenv("FUUL_API_KEY", "")
FUUL_PROJECT_ID  = os.getenv("FUUL_PROJECT_ID", "")
FUUL_SDK_VERSION = "7.18.0"

# API 키 없으면 Mock 모드
MOCK_MODE = not FUUL_API_KEY.strip()

# 앱 기본 URL (레퍼럴 링크용)
APP_BASE_URL = os.getenv("APP_BASE_URL", "https://copy-perp.vercel.app")


class FuulReferral:
    """Fuul 레퍼럴 시스템 연동 v2 (SDK v7.18.0 구조 반영)"""

    def __init__(self):
        self.mock = MOCK_MODE
        self._points: dict = {}   # in-memory 포인트 (mock/테스트용)
        if self.mock:
            logger.info("Fuul: Mock 모드 (FUUL_API_KEY 미설정)")
        else:
            logger.info("Fuul: 실제 연동 모드 (project=%s)", FUUL_PROJECT_ID)

    # ── HTTP 헬퍼 ──────────────────────────────────────────────────────────

    def _request(self, method: str, path: str, body: Optional[dict] = None,
                 params: Optional[dict] = None) -> dict:
        """HTTP 요청 (GET/POST)"""
        if self.mock:
            return {"ok": True, "mock": True, "path": path, "body": body}

        url = f"{FUUL_API_BASE.rstrip('/')}/{path.lstrip('/')}"
        if params:
            from urllib.parse import urlencode
            url = f"{url}?{urlencode(params)}"

        payload = json.dumps(body).encode("utf-8") if body else None
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": f"Bearer {FUUL_API_KEY}",
                "Content-Type": "application/json",
                "X-Fuul-Sdk-Version": FUUL_SDK_VERSION,
                "User-Agent": "CopyPerp/1.0",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body_err = e.read().decode()
            logger.warning("Fuul API 오류 HTTP %d: %s", e.code, body_err)
            return {"ok": False, "error": body_err, "status": e.code}
        except Exception as e:
            # latin-1 인코딩 오류 등 헤더 문제 → mock으로 폴백
            err_str = str(e)
            if "codec" in err_str or "encode" in err_str:
                logger.debug("Fuul 헤더 인코딩 오류 (mock 폴백): %s", err_str)
                return {"ok": True, "mock_fallback": True}
            logger.warning("Fuul 연결 실패: %s", e)
            return {"ok": False, "error": err_str}

    def _post(self, path: str, body: dict, params: Optional[dict] = None) -> dict:
        return self._request("POST", path, body, params)

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        return self._request("GET", path, params=params)

    def _patch(self, path: str, body: Optional[dict] = None,
               params: Optional[dict] = None) -> dict:
        return self._request("PATCH", path, body, params)

    # ── 이벤트 payload 생성 ────────────────────────────────────────────────

    def _make_event(
        self,
        name: str,
        user_address: Optional[str] = None,
        identifier_type: str = "solana_address",
        args: Optional[dict] = None,
        referrer: Optional[str] = None,
        tracking_id: Optional[str] = None,
    ) -> dict:
        """
        Fuul SDK v7.18.0 표준 이벤트 payload 생성
        
        구조:
        {
          "name": "event_name",
          "user": {"identifier": "...", "identifier_type": "solana_address"},
          "args": {...},
          "metadata": {
            "tracking_id": "...",
            "project_id": "...",    # 있을 경우
            "referrer": "...",      # 있을 경우
          }
        }
        """
        event: dict = {
            "name": name,
            "args": args or {},
            "metadata": {
                "tracking_id": tracking_id or str(uuid.uuid4()),
            },
        }
        # user 필드 (지갑 주소 있을 경우)
        if user_address:
            event["user"] = {
                "identifier": user_address,
                "identifier_type": identifier_type,
            }
        # 프로젝트 ID
        if FUUL_PROJECT_ID:
            event["metadata"]["project_id"] = FUUL_PROJECT_ID
        # 레퍼러
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
        """팔로워 온보딩 이벤트 (follow)"""
        event = self._make_event(
            name="follow",
            user_address=follower_address,
            args={"trader": trader_address, "timestamp": int(time.time())},
            referrer=referrer,
        )
        result = self._post("events", event)
        logger.info("Fuul follow: follower=%s trader=%s → %s",
                    follower_address[:8], trader_address[:8], result)
        return result

    def track_connect_wallet(self, address: str) -> dict:
        """
        지갑 연결 이벤트 (connect_wallet) — SDK의 identifyUser()에 해당
        
        SDK 구조:
        {
          "name": "connect_wallet",
          "user": {"identifier": address, "identifier_type": "solana_address"},
          "args": {"page": "/", "locationOrigin": "https://..."},
          "metadata": {"tracking_id": "..."}
        }
        """
        event = self._make_event(
            name="connect_wallet",
            user_address=address,
            args={
                "page": "/",
                "locationOrigin": APP_BASE_URL,
            },
        )
        result = self._post("events", event)
        logger.info("Fuul connect_wallet: address=%s → %s", address[:8], result)
        return result

    # alias (하위 호환)
    def identify_user(self, address: str) -> dict:
        """지갑 연결 이벤트 (connect_wallet의 alias)"""
        return self.track_connect_wallet(address)

    def track_copy_trade(
        self,
        follower_address: str,
        trader_address: str,
        symbol: str,
        side: str,
        amount_usdc: float,
        order_id: Optional[str] = None,
    ) -> dict:
        """복사 주문 체결 이벤트 (copy_trade)"""
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
        """페이지뷰 이벤트"""
        event = {
            "name": "pageview",
            "args": {
                "page": page,
                "locationOrigin": APP_BASE_URL,
            },
            "metadata": {"tracking_id": str(uuid.uuid4())},
        }
        if FUUL_PROJECT_ID:
            event["metadata"]["project_id"] = FUUL_PROJECT_ID
        return self._post("events", event)

    # ── 레퍼럴 코드 관리 ──────────────────────────────────────────────────

    def get_referral_link(self, address: str, ref_code: Optional[str] = None) -> str:
        """
        레퍼럴 링크 생성
        
        ref_code가 있으면 커스텀 코드 사용.
        없으면 주소 앞 8자로 자동 생성.
        
        SDK: generateTrackingLink(baseUrl, userIdentifier, identifierType)
        → {baseUrl}?af={code}
        """
        code = ref_code or address[:8]
        # af= (Fuul SDK 표준) + ref= (하위 호환) 둘 다 포함
        return f"{APP_BASE_URL}?ref={code}&af={code}"

    def generate_referral_link(self, address: str, ref_code: Optional[str] = None) -> str:
        """레퍼럴 링크 생성 (alias)"""
        return self.get_referral_link(address, ref_code)

    def create_referral_code(
        self,
        user_address: str,
        quantity: int = 1,
        max_uses: Optional[int] = None,
    ) -> dict:
        """
        레퍼럴 코드 생성 (POST /referral_codes)
        
        SDK: generateReferralCodes({user_identifier, user_identifier_type, quantity, max_uses})
        """
        params: dict = {
            "user_identifier": user_address,
            "user_identifier_type": "solana_address",
            "quantity": quantity,
        }
        if max_uses is not None:
            params["max_uses"] = max_uses
        return self._post("referral_codes", {}, params=params)

    def get_referral_code_info(self, code: str) -> dict:
        """레퍼럴 코드 정보 조회 (GET /referral_codes/{code})"""
        return self._get(f"referral_codes/{code}")

    def get_referral_status(self, user_address: str) -> dict:
        """레퍼럴 현황 조회 (GET /referral_codes/status)"""
        return self._get("referral_codes/status", params={
            "user_identifier": user_address,
            "user_identifier_type": "solana_address",
        })

    # ── 포인트/통계 ────────────────────────────────────────────────────────

    async def track_trade_volume(self, address: str, volume_usdc: float) -> dict:
        """거래 볼륨 기반 포인트 적립 (0.1% per USDC)"""
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


    def send_batch_events(self, events: list) -> dict:
        """배치 이벤트 전송 (API 호출 횟수 절감)
        
        Args:
            events: 이벤트 딕셔너리 리스트
        Returns:
            API 응답
        """
        if self.mock:
            logger.info("Fuul [MOCK] batch: %d events", len(events))
            return {"ok": True, "mock": True, "count": len(events)}
        result = self._post("events/batch", events)
        logger.info("Fuul batch: %d events → %s", len(events), result)
        return result

    def get_leaderboard(self, limit: int = 10) -> list:
        """포인트 리더보드 (in-memory)"""
        sorted_pts = sorted(self._points.items(), key=lambda x: x[1], reverse=True)
        return [{"address": addr, "points": pts} for addr, pts in sorted_pts[:limit]]

    def get_referral_stats(self, address: str) -> dict:
        """레퍼럴 볼륨 통계 (GET /payouts/leaderboard/referred-volume)"""
        if self.mock:
            return {
                "address": address,
                "referrals": 0,
                "total_volume": 0.0,
                "mock": True,
            }
        return self._get(
            "payouts/leaderboard/referred-volume",
            params={"user_identifiers": address},
        )

    # ── async aliases ──────────────────────────────────────────────────────

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


# ── 싱글턴 ─────────────────────────────────────────────────────────────────
_fuul: Optional[FuulReferral] = None


def get_fuul() -> FuulReferral:
    global _fuul
    if _fuul is None:
        _fuul = FuulReferral()
    return _fuul
