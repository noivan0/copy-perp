"""
Position Monitor — 트레이더 포지션 변화 감시
전략: WebSocket account 이벤트 구독 (지원 시) / REST 폴링 (백업)
"""
import asyncio
import json
import logging
from typing import Callable

import websockets

from core.copy_engine import CopyEngine, FillEvent
from db.models import get_active_followers

log = logging.getLogger(__name__)

WS_URL = "wss://test-ws.pacifica.fi/ws"  # 테스트넷


class PositionMonitor:
    """
    등록된 트레이더들의 포지션 변화 실시간 감시.
    
    W1 Day 1 확인 필요:
    - Pacifica WS에서 account_fills 구독 지원 여부
    - 미지원 시 REST 폴링 (500ms) 으로 대체
    """

    def __init__(self, copy_engine: CopyEngine):
        self.engine = copy_engine
        self._traders: set[str] = set()
        self._running = False
        self._positions_cache: dict = {}  # trader_id → 마지막 포지션

    def add_trader(self, trader_id: str):
        self._traders.add(trader_id)
        log.info(f"트레이더 모니터링 추가: {trader_id}")

    async def start(self):
        self._running = True
        # WebSocket 시도 → 실패 시 폴링으로 폴백
        try:
            await self._watch_via_websocket()
        except Exception as e:
            log.warning(f"WS 감시 실패({e}), REST 폴링으로 전환")
            await self._watch_via_polling()

    async def _watch_via_websocket(self):
        """
        방식 1: WebSocket account_fills 구독
        W1 Day 1에 실제 지원 여부 확인
        """
        async with websockets.connect(WS_URL, ping_interval=30) as ws:
            for trader_id in self._traders:
                await ws.send(json.dumps({
                    "method": "subscribe",
                    "params": {
                        "source": "account_fills",  # 확인 필요
                        "account": trader_id,
                    }
                }))
            log.info(f"WS 감시 시작 — traders={len(self._traders)}")

            async for raw in ws:
                msg = json.loads(raw)
                await self._handle_ws_event(msg)

    async def _watch_via_polling(self):
        """
        방식 2: REST 폴링 (WS 미지원 시 백업)
        500ms 간격으로 포지션 변화 감지
        """
        from pacifica.client import PacificaClient

        log.info("REST 폴링 감시 시작")
        while self._running:
            for trader_id in list(self._traders):
                try:
                    client = PacificaClient(account_address=trader_id)
                    positions = await client.get_positions()

                    prev = self._positions_cache.get(trader_id, {})
                    changes = self._detect_changes(prev, positions)

                    for change in changes:
                        event = FillEvent(
                            account=trader_id,
                            symbol=change["symbol"],
                            side=change["side"],
                            amount=change["amount"],
                            order_id=change.get("order_id", "polling"),
                            price=change.get("price", 0),
                        )
                        await self.engine.on_fill(event)

                    self._positions_cache[trader_id] = positions

                except Exception as e:
                    log.error(f"폴링 오류 trader={trader_id}: {e}")

            await asyncio.sleep(0.5)  # 500ms

    def _detect_changes(self, prev: dict, curr: dict) -> list:
        """이전/현재 포지션 비교 → 변화 감지"""
        changes = []
        for symbol, pos in curr.items():
            prev_pos = prev.get(symbol, {})
            if pos.get("size") != prev_pos.get("size"):
                size_diff = float(pos.get("size", 0)) - float(prev_pos.get("size", 0))
                if size_diff != 0:
                    changes.append({
                        "symbol": symbol,
                        "side": "bid" if size_diff > 0 else "ask",
                        "amount": str(abs(size_diff)),
                        "price": pos.get("mark_price", 0),
                    })
        return changes

    async def _handle_ws_event(self, msg: dict):
        """WS 이벤트 파싱 → FillEvent 변환"""
        # W1 Day 1: 실제 이벤트 구조 확인 후 파싱 로직 완성
        if msg.get("type") == "fill":
            event = FillEvent(
                account=msg["account"],
                symbol=msg["symbol"],
                side=msg["side"],
                amount=msg["amount"],
                order_id=msg["order_id"],
                price=msg.get("price", 0),
            )
            await self.engine.on_fill(event)

    def stop(self):
        self._running = False
