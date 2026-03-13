"""
Position Monitor v3 — 확인된 WS source 기반
테스트 결과:
  ✅ account_positions → 실제 데이터 수신 확인
  ✅ account_order_updates → 구독 확인 (이벤트 발생 시 수신)
  ✅ account_trades → 구독 확인 (이벤트 발생 시 수신)
  ❌ account_fills, account_orders → 구독 확인 안 됨

플로우:
1. WS account_positions + account_trades 구독
2. 포지션 변화 감지 → on_fill 콜백
3. 실패 시 REST 500ms 폴링 폴백
"""

import asyncio
import json
import logging
import ssl
import time
from typing import Callable, Optional

import websockets

logger = logging.getLogger(__name__)

import os as _os
WS_URL = _os.getenv("PACIFICA_WS_URL", "wss://ws.pacifica.fi/ws")
REST_POLL_INTERVAL = 0.5  # 500ms

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

# 확인된 WS account 소스
WS_ACCOUNT_SOURCES = [
    "account_positions",    # ✅ 실제 데이터 수신 확인
    "account_order_updates", # ✅ 구독 확인
    "account_trades",       # ✅ 구독 확인
]


class PositionMonitor:
    """트레이더 포지션 변화 감지 → on_fill 콜백
    
    WS 연결 시도 순서:
    1. wss://test-ws.pacifica.fi/ws (직접)
    2. 실패 시 → RestPositionMonitor 자동 전환 (HMG 웹필터 우회)
    """

    # WS 시도 URL 목록 (순서대로 시도)
    _WS_URLS = [
        "wss://test-ws.pacifica.fi/ws",
        WS_URL,
    ]

    def __init__(self, trader_address: str, on_fill: Callable):
        self.trader = trader_address
        self.on_fill = on_fill
        self._running = False
        self._prev_positions: dict = {}  # symbol → position
        self._reconnect_delay = 2.0
        self._rest_fallback: Optional["RestPositionMonitor"] = None
        self._ws_failed = False  # WS 연결 최종 실패 여부

    async def start(self):
        self._running = True
        logger.info(f"PositionMonitor 시작: {self.trader[:12]}...")

        # WS 연결 시도
        ws_connected = False
        for ws_url in self._WS_URLS:
            if not self._running:
                break
            try:
                logger.info(f"WS 연결 시도: {ws_url}")
                await asyncio.wait_for(
                    self._ws_loop(ws_url),
                    timeout=10.0  # 10초 내 연결 안 되면 다음 URL 시도
                )
                ws_connected = True
                break
            except asyncio.TimeoutError:
                logger.warning(f"WS 연결 타임아웃: {ws_url} → 다음 URL 시도")
            except Exception as e:
                logger.warning(f"WS 연결 실패: {ws_url} ({type(e).__name__}: {e})")

        if not ws_connected and self._running:
            # 모든 WS URL 실패 → RestPositionMonitor로 자동 전환
            logger.warning(
                f"[{self.trader[:12]}] 모든 WS URL 실패 → RestPositionMonitor 자동 전환"
            )
            self._ws_failed = True
            self._rest_fallback = RestPositionMonitor(self.trader, self.on_fill)
            # 이미 수집된 prev_positions 상태 동기화
            self._rest_fallback._prev_positions = self._prev_positions
            await self._rest_fallback.start()
            return

        # WS 성공했다가 이후 재연결 루프
        while self._running and not self._ws_failed:
            try:
                await self._ws_loop(WS_URL)
            except Exception as e:
                if self._running:
                    logger.warning(f"WS 오류 ({e}), {self._reconnect_delay}초 후 재연결 → REST 폴링 전환")
                    await self._rest_poll_burst(duration=self._reconnect_delay)
                    self._reconnect_delay = min(self._reconnect_delay * 1.5, 30.0)

    async def stop(self):
        self._running = False
        if self._rest_fallback:
            await self._rest_fallback.stop()

    async def _ws_loop(self, ws_url: Optional[str] = None):
        url = ws_url or WS_URL
        async with websockets.connect(
            url,
            ssl=_ssl_ctx,
            ping_interval=20,
            ping_timeout=10,
        ) as ws:
            # 확인된 소스 구독
            for source in WS_ACCOUNT_SOURCES:
                await ws.send(json.dumps({
                    "method": "subscribe",
                    "params": {"source": source, "account": self.trader}
                }))
            logger.info(f"WS 구독 완료: {WS_ACCOUNT_SOURCES} (url={url})")
            self._reconnect_delay = 2.0  # 성공 시 초기화

            async for raw in ws:
                if not self._running:
                    break
                data = json.loads(raw)
                await self._dispatch(data)

    async def _dispatch(self, data: dict):
        channel = data.get("channel", "")

        if channel == "account_positions":
            raw = data.get("data", [])
            # data가 dict인 경우 (단일 포지션) → list로 변환
            if isinstance(raw, dict):
                raw = [raw]
            elif not isinstance(raw, list):
                raw = []
            await self._handle_positions(raw)

        elif channel == "account_trades":
            items = data.get("data", [])
            if isinstance(items, list):
                for item in items:
                    await self._emit_fill(item)
            elif isinstance(items, dict):
                await self._emit_fill(items)

        elif channel == "account_order_updates":
            # 주문 체결 이벤트
            item = data.get("data", {})
            if item.get("status") in ("filled", "partially_filled"):
                await self._emit_fill(item)

    async def _handle_positions(self, positions: list):
        """포지션 변화 감지 (스냅샷 비교)"""
        curr = {p.get("symbol"): p for p in positions if p.get("symbol")}

        for symbol, pos in curr.items():
            prev = self._prev_positions.get(symbol)
            curr_size = float(pos.get("szi", pos.get("size", 0)) or 0)
            prev_size = float(prev.get("szi", prev.get("size", 0)) or 0) if prev else 0

            if abs(curr_size - prev_size) > 1e-8:
                change_type = "open" if abs(curr_size) > abs(prev_size) else "reduce"
                side = "open_long" if curr_size > prev_size else "open_short"

                fill_event = {
                    "account": self.trader,
                    "symbol": symbol,
                    "event_type": "position_change",
                    "side": side,
                    "amount": str(abs(curr_size - prev_size)),
                    "price": pos.get("entry_price", "0"),
                    "cause": "normal",
                    "created_at": int(time.time() * 1000),
                    "change_type": change_type,
                }
                logger.info(f"포지션 변화 감지: {symbol} {side} Δ{abs(curr_size-prev_size):.4f}")
                await self._emit_fill(fill_event)

        # 포지션 청산 감지
        for symbol in list(self._prev_positions.keys()):
            if symbol not in curr:
                prev = self._prev_positions[symbol]
                prev_size = float(prev.get("szi", 0) or 0)
                side = "close_long" if prev_size > 0 else "close_short"
                fill_event = {
                    "account": self.trader,
                    "symbol": symbol,
                    "event_type": "position_closed",
                    "side": side,
                    "amount": str(abs(prev_size)),
                    "price": "0",
                    "cause": "normal",
                    "created_at": int(time.time() * 1000),
                }
                logger.info(f"포지션 청산 감지: {symbol} {side}")
                await self._emit_fill(fill_event)

        self._prev_positions = curr

    async def _emit_fill(self, event: dict):
        event["account"] = event.get("account") or self.trader
        try:
            await self.on_fill(event)
        except Exception as e:
            logger.error(f"on_fill 오류: {e}")

    async def _rest_poll_burst(self, duration: float):
        """WS 재연결 대기 동안 REST 폴링"""
        from pacifica.client import PacificaClient
        client = PacificaClient(self.trader)
        deadline = time.time() + duration
        while time.time() < deadline and self._running:
            try:
                positions = client.get_positions()
                await self._handle_positions(positions)
            except Exception:
                pass
            await asyncio.sleep(REST_POLL_INTERVAL)


# ── 테스트 ──────────────────────────────────
if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()

    ACCOUNT = os.getenv("ACCOUNT_ADDRESS", "3AHZqrocSguMuo9sUUP8G8YN8NwHwWV2DPUQvbDvtfaQ")

    async def on_fill(event):
        print(f"[FILL] {json.dumps(event)}")

    async def main():
        monitor = PositionMonitor(ACCOUNT, on_fill)
        print(f"모니터링: {ACCOUNT[:16]}... (10초)")
        try:
            await asyncio.wait_for(monitor.start(), timeout=10)
        except asyncio.TimeoutError:
            await monitor.stop()
            print("✅ 10초 완료 — 포지션 변화 없음 (잔고 0 상태 정상)")

    asyncio.run(main())


class RestPositionMonitor(PositionMonitor):
    """REST 폴링 전용 PositionMonitor (WS 차단 환경용)
    
    WS 연결을 시도하지 않고 REST 폴링만 사용.
    HMG 웹필터 환경에서 안정적으로 동작.
    연속 5회 이상 실패 시 60초 대기 후 자동 재시작.
    """

    def __init__(self, trader_address: str, on_fill: Callable):
        super().__init__(trader_address, on_fill)
        self._fail_count = 0
        self._last_poll_time: Optional[float] = None  # 마지막 폴링 성공 시각 (epoch)

    async def start(self):
        self._running = True
        logger.info(f"[REST] PositionMonitor 시작: {self.trader[:12]}...")
        from pacifica.client import PacificaClient
        client = PacificaClient(self.trader)

        while self._running:
            try:
                positions = await asyncio.get_event_loop().run_in_executor(
                    None, client.get_positions
                )
                await self._handle_positions(positions)
                self._fail_count = 0  # 성공 시 카운터 초기화
                self._last_poll_time = time.time()
            except Exception as e:
                self._fail_count += 1
                logger.debug(f"REST 폴링 오류 (연속 {self._fail_count}회): {e}")
                if self._fail_count > 5:
                    logger.warning(
                        f"[REST] {self.trader[:12]} 연속 {self._fail_count}회 실패 — "
                        f"60초 대기 후 재시작"
                    )
                    try:
                        from core.alerting import get_alert_manager
                        get_alert_manager().monitor_disconnected(self.trader, str(e))
                    except Exception:
                        pass
                    await asyncio.sleep(60)
                    self._fail_count = 0
                    client = PacificaClient(self.trader)  # 클라이언트 재생성
                    logger.info(f"[REST] {self.trader[:12]} 재시작")
                    try:
                        from core.alerting import get_alert_manager
                        get_alert_manager().monitor_restored(self.trader)
                    except Exception:
                        pass
                    continue
            await asyncio.sleep(2.0)  # 2초 간격 폴링
