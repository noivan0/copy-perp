"""
Position Monitor v2 — 트레이더 체결 이벤트 감지

전략:
1. WS account_trades 구독 (실시간, 지연 ~50ms) — 우선
2. REST /trades 500ms 폴링 (폴백)

WS account_trades 채널 확인됨:
  subscribe → {"channel": "subscribe", "data": {"source": "account_trades", "account": "..."}}
  체결 시 → {"channel": "account_trades", "data": {...fill event...}} (예상)
"""

import asyncio
import json
import logging
import ssl
import time
from typing import Callable, Optional

import websockets

logger = logging.getLogger(__name__)

WS_URL = "wss://test-ws.pacifica.fi/ws"

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


class PositionMonitor:
    """
    트레이더 체결 이벤트 감지 → on_fill 콜백 호출
    WS 우선, 실패 시 REST 500ms 폴링으로 자동 전환
    """

    def __init__(self, trader_address: str, on_fill: Callable):
        self.trader = trader_address
        self.on_fill = on_fill
        self._running = False
        self._use_ws = True  # WS 우선 시도

    async def start(self):
        self._running = True
        logger.info(f"PositionMonitor 시작: {self.trader[:12]}...")

        while self._running:
            if self._use_ws:
                try:
                    await self._ws_loop()
                except Exception as e:
                    logger.warning(f"WS 연결 끊김 ({e}), REST 폴링으로 전환")
                    self._use_ws = False
            else:
                try:
                    await self._rest_polling()
                except Exception as e:
                    logger.error(f"REST 폴링 오류: {e}")
                    await asyncio.sleep(2)

    async def stop(self):
        self._running = False
        logger.info(f"PositionMonitor 중지: {self.trader[:12]}...")

    # ── WS 모드 ────────────────────────────────────────
    async def _ws_loop(self):
        """WS account_trades 구독 → 체결 이벤트 실시간 수신"""
        async with websockets.connect(WS_URL, ssl=_ssl_ctx, ping_interval=20) as ws:
            sub = {
                "method": "subscribe",
                "params": {"source": "account_trades", "account": self.trader}
            }
            await ws.send(json.dumps(sub))
            logger.info(f"WS account_trades 구독: {self.trader[:12]}...")

            async for raw in ws:
                if not self._running:
                    break
                data = json.loads(raw)
                await self._handle_ws_event(data)

    async def _handle_ws_event(self, data: dict):
        """WS 이벤트 파싱 — account_trades 채널 체결 이벤트"""
        channel = data.get("channel", "")

        if channel == "account_trades":
            payload = data.get("data", {})
            if isinstance(payload, dict):
                # 단건 체결
                if payload.get("event_type") in ("fulfill_taker", "fulfill_maker"):
                    payload.setdefault("account", self.trader)
                    logger.info(f"[WS] 체결: {payload.get('side')} {payload.get('amount')} {payload.get('symbol')}")
                    await self.on_fill(payload)
            elif isinstance(payload, list):
                # 복수 체결
                for item in payload:
                    if item.get("event_type") in ("fulfill_taker", "fulfill_maker"):
                        item.setdefault("account", self.trader)
                        await self.on_fill(item)

        elif channel == "subscribe":
            logger.debug(f"WS 구독 확인: {data.get('data', {}).get('source')}")

    # ── REST 폴링 모드 (폴백) ──────────────────────────
    async def _rest_polling(self):
        """REST /trades?account=... 500ms 폴링"""
        from pacifica.client import PacificaClient
        client = PacificaClient(self.trader)

        logger.info(f"REST 폴링 시작 (500ms): {self.trader[:12]}...")
        prev_keys: set = set()

        # 첫 폴링으로 기존 체결 baseline 설정
        try:
            initial = client.get_account_trades(limit=20)
            prev_keys = {self._trade_key(t) for t in initial}
        except Exception as e:
            logger.warning(f"초기 체결 조회 실패: {e}")

        while self._running:
            await asyncio.sleep(0.5)
            try:
                trades = client.get_account_trades(limit=20)
                new_keys = {self._trade_key(t) for t in trades}
                new_trades = [t for t in trades if self._trade_key(t) not in prev_keys]

                for t in new_trades:
                    t.setdefault("account", self.trader)
                    logger.info(f"[REST] 새 체결: {t.get('side')} {t.get('amount')}")
                    await self.on_fill(t)

                prev_keys = new_keys
            except Exception as e:
                logger.error(f"폴링 오류: {e}")

    @staticmethod
    def _trade_key(trade: dict) -> tuple:
        """체결 고유 키 (created_at + price + amount)"""
        return (trade.get("created_at"), trade.get("price"), trade.get("amount"), trade.get("side"))


# ── 테스트 ───────────────────────────────────────────
if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()

    ACCOUNT = os.getenv("ACCOUNT_ADDRESS", "3AHZqrocSguMuo9sUUP8G8YN8NwHwWV2DPUQvbDvtfaQ")

    async def on_fill(event):
        print(f"[FILL] {json.dumps(event, ensure_ascii=False)}")

    async def main():
        monitor = PositionMonitor(ACCOUNT, on_fill)
        print(f"모니터링 시작: {ACCOUNT[:12]}... (Ctrl+C 종료)")
        try:
            await monitor.start()
        except KeyboardInterrupt:
            await monitor.stop()

    asyncio.run(main())
