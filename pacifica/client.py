"""
Pacifica Testnet REST + WebSocket 클라이언트
"""
import asyncio
import hashlib
import hmac
import json
import os
import time
import uuid
from typing import Optional

import aiohttp

REST_URL = os.getenv("PACIFICA_REST_URL", "https://test-api.pacifica.fi/api/v1")
WS_URL = os.getenv("PACIFICA_WS_URL", "wss://test-ws.pacifica.fi/ws")
BUILDER_CODE = os.getenv("BUILDER_CODE", "")

# CEO 지갑 (조회용)
CEO_WALLET = "3AHZqrocSguMuo9sUUP8G8YN8NwHwWV2DPUQvbDvtfaQ"


class PacificaClient:
    """
    Pacifica Testnet API 클라이언트
    - account_address: 조회 대상 지갑 주소
    - private_key: 주문 실행 시 필요 (선택)
    """

    def __init__(self, account_address: str, private_key: Optional[str] = None):
        self.account = account_address
        self.private_key = private_key
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=False)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ─── 조회 (서명 불필요) ──────────────────────────────

    async def get_info(self) -> dict:
        """거래소 기본 정보"""
        s = await self._get_session()
        async with s.get(f"{REST_URL}/info") as r:
            return await r.json()

    async def get_balance(self) -> float:
        """계정 잔고 (USD)"""
        s = await self._get_session()
        params = {"account": self.account}
        async with s.get(f"{REST_URL}/clearinghouse_state", params=params) as r:
            data = await r.json()
            # marginSummary.accountValue 또는 crossMarginSummary 확인
            summary = data.get("marginSummary") or data.get("crossMarginSummary", {})
            return float(summary.get("accountValue", 0))

    async def get_positions(self) -> list:
        """현재 포지션 목록"""
        s = await self._get_session()
        params = {"account": self.account}
        async with s.get(f"{REST_URL}/clearinghouse_state", params=params) as r:
            data = await r.json()
            return data.get("assetPositions", [])

    async def get_leaderboard(self, window: str = "day") -> dict:
        """리더보드 조회"""
        s = await self._get_session()
        payload = {"type": "leaderboard", "window": window}
        async with s.post(f"{REST_URL}/info", json=payload) as r:
            return await r.json()

    # ─── 주문 (서명 필요 — W1에서 SDK 연동 후 구현) ───────

    async def market_order(
        self,
        symbol: str,
        side: str,          # "bid" (롱) / "ask" (숏)
        amount: str,        # USD 기준
        builder_code: str = BUILDER_CODE,
        client_order_id: Optional[str] = None,
    ) -> dict:
        """
        시장가 주문 실행
        TODO: Pacifica Python SDK 서명 로직 연동 (3/16)
        """
        if not self.private_key:
            raise ValueError("주문 실행에는 private_key가 필요합니다")

        order_id = client_order_id or str(uuid.uuid4())

        # 실제 SDK 연동 전 플레이스홀더
        # from pacifica_sdk import sign_order
        # signed = sign_order(...)
        raise NotImplementedError("SDK 서명 로직 — 3/16 개발 시작 시 구현 예정")

    async def approve_builder_code(self, builder_code: str, max_fee_rate: str = "0.001") -> dict:
        """
        Builder Code 승인 (팔로워가 최초 1회 실행)
        POST /api/v1/account/builder_codes/approve
        """
        if not self.private_key:
            raise ValueError("승인에는 private_key가 필요합니다")
        raise NotImplementedError("SDK 서명 로직 — 3/16 개발 시작 시 구현 예정")


async def check_ceo_wallet():
    """CEO 지갑 상태 확인 (테스트용)"""
    client = PacificaClient(account_address=CEO_WALLET)
    try:
        info = await client.get_info()
        print(f"✅ API 연결 OK — universe: {len(info.get('universe', []))}개 심볼")
        balance = await client.get_balance()
        print(f"💰 CEO 지갑 잔고: ${balance:,.2f}")
        positions = await client.get_positions()
        print(f"📊 현재 포지션: {len(positions)}개")
        for p in positions:
            print(f"  • {p}")
    except Exception as e:
        print(f"❌ 오류: {e}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(check_ceo_wallet())
