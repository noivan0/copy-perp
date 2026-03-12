"""
Position Monitor — 트레이더 포지션 변화 감지
WS 구독: {"method": "subscribe", "params": {"source": "prices"}} 확인됨
account fill 이벤트: WS source 파라미터 미확인 → REST 500ms 폴링 병행

실제 구조 (SDK ws/subscribe_prices.py 기반):
  {"method": "subscribe", "params": {"source": "prices"}}
  {"method": "subscribe", "params": {"source": "account", "account": "..."}} ← 시도
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
    트레이더의 포지션 변화를 감지하고 콜백 호출
    
    우선: WS account 이벤트 구독 시도
    폴백: REST 500ms 폴링
    """

    def __init__(self, trader_address: str, on_fill: Callable):
        self.trader = trader_address
        self.on_fill = on_fill  # async 콜백: on_fill(fill_event: dict)
        self._running = False
        self._positions_cache: dict = {}  # symbol → position snapshot

    async def start(self):
        self._running = True
        logger.info(f"PositionMonitor 시작: {self.trader[:8]}...")
        
        # WS 먼저 시도, 실패 시 REST 폴링
        try:
            await self._ws_subscribe()
        except Exception as e:
            logger.warning(f"WS 구독 실패 ({e}), REST 폴링으로 전환")
            await self._rest_polling()

    async def stop(self):
        self._running = False

    async def _ws_subscribe(self):
        """WS account 이벤트 구독 시도"""
        async with websockets.connect(
            WS_URL,
            ping_interval=30,
            ssl=_ssl_ctx
        ) as ws:
            # SDK 확인된 패턴: account_twap_orders, account_twap_order_updates
            # fills 관련 유사 패턴 시도
            for source in ["account_order_updates", "account_fills", "account_orders",
                           "account_positions", "account_trades"]:
                sub_msg = {
                    "method": "subscribe",
                    "params": {"source": source, "account": self.trader}
                }
                await ws.send(json.dumps(sub_msg))

            logger.info("WS account 구독 전송 (fills/account 시도)")

            async for raw in ws:
                if not self._running:
                    break
                data = json.loads(raw)
                await self._handle_ws_event(data)

    async def _handle_ws_event(self, data: dict):
        """WS 이벤트 처리"""
        # fills 이벤트 감지
        if data.get("type") in ("fill", "account_fill", "order_fill"):
            logger.info(f"WS fill 이벤트: {data}")
            await self.on_fill(data)
        elif data.get("data"):
            inner = data["data"]
            if isinstance(inner, dict) and inner.get("event_type") in ("fulfill_taker", "fulfill_maker"):
                await self.on_fill(inner)

    async def _rest_polling(self):
        """REST 500ms 폴링 (WS 실패 시 폴백)"""
        from pacifica.client import PacificaClient
        client = PacificaClient(self.trader)
        
        logger.info("REST 폴링 시작 (500ms)")
        prev_trades: list = []

        while self._running:
            try:
                trades = client.get_trades(limit=10)
                
                if prev_trades:
                    # 새 체결 감지
                    prev_ids = {(t.get("created_at"), t.get("price"), t.get("amount")) for t in prev_trades}
                    for t in trades:
                        key = (t.get("created_at"), t.get("price"), t.get("amount"))
                        if key not in prev_ids:
                            logger.info(f"새 체결 감지: {t}")
                            await self.on_fill(t)
                
                prev_trades = trades
            except Exception as e:
                logger.error(f"REST 폴링 오류: {e}")

            await asyncio.sleep(0.5)


# ── 테스트 ──────────────────────────────────
if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()

    ACCOUNT = os.getenv("ACCOUNT_ADDRESS", "3AHZqrocSguMuo9sUUP8G8YN8NwHwWV2DPUQvbDvtfaQ")

    async def on_fill(event):
        print(f"[FILL] {json.dumps(event, indent=2)}")

    async def main():
        monitor = PositionMonitor(ACCOUNT, on_fill)
        print(f"모니터링 시작: {ACCOUNT}")
        print("Ctrl+C로 종료")
        try:
            await monitor.start()
        except KeyboardInterrupt:
            await monitor.stop()

    asyncio.run(main())
