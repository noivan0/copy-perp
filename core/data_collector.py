"""
Data Collector — WS 실시간 시장 데이터 수집
확인된 WS 구조:
  subscribe: {"method": "subscribe", "params": {"source": "prices"}}
  데이터: {"symbol":"BTC","funding":"0.00001452","oracle":"...","mark":"...","open_interest":"..."}

수집 항목:
- 가격 (mark, oracle, mid)
- 펀딩비 (funding, next_funding)
- 오픈 인터레스트
- Oracle-Mark 괴리율 (시그널용)
"""

import asyncio
import json
import logging
import ssl
import time
from collections import defaultdict
from typing import Optional

import websockets
import aiosqlite

logger = logging.getLogger(__name__)

WS_URL = "wss://test-ws.pacifica.fi/ws"

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


class DataCollector:
    """
    실시간 시장 데이터 수집 및 시그널 계산
    """

    def __init__(self, db: Optional[aiosqlite.Connection] = None):
        self.db = db
        self._running = False
        # 최신 시장 데이터 캐시 (symbol → dict)
        self.market_data: dict[str, dict] = {}

    async def start(self):
        self._running = True
        logger.info("DataCollector 시작")
        while self._running:
            try:
                await self._collect()
            except Exception as e:
                logger.error(f"WS 오류: {e}, 5초 후 재연결")
                await asyncio.sleep(5)

    async def stop(self):
        self._running = False

    async def _collect(self):
        async with websockets.connect(WS_URL, ssl=_ssl_ctx, ping_interval=20) as ws:
            await ws.send(json.dumps({
                "method": "subscribe",
                "params": {"source": "prices"}
            }))
            logger.info("WS prices 구독 완료")

            async for raw in ws:
                if not self._running:
                    break
                data = json.loads(raw)
                if data.get("channel") == "prices" or isinstance(data.get("data"), list):
                    items = data.get("data", [])
                    if isinstance(items, list):
                        for item in items:
                            await self._process_tick(item)

    async def _process_tick(self, tick: dict):
        symbol = tick.get("symbol")
        if not symbol:
            return

        mark = float(tick.get("mark", 0) or 0)
        oracle = float(tick.get("oracle", 0) or 0)
        funding = float(tick.get("funding", 0) or 0)
        oi = float(tick.get("open_interest", 0) or 0)
        ts = tick.get("timestamp", int(time.time() * 1000))

        # Oracle-Mark 괴리율 계산
        divergence = ((mark - oracle) / oracle) if oracle > 0 else 0

        self.market_data[symbol] = {
            "symbol": symbol,
            "mark": mark,
            "oracle": oracle,
            "mid": float(tick.get("mid", 0) or 0),
            "funding": funding,
            "next_funding": float(tick.get("next_funding", 0) or 0),
            "open_interest": oi,
            "volume_24h": float(tick.get("volume_24h", 0) or 0),
            "divergence": divergence,
            "timestamp": ts,
        }

        # 시그널 체크
        await self._check_signals(symbol)

    async def _check_signals(self, symbol: str):
        d = self.market_data.get(symbol)
        if not d:
            return

        # 펀딩비 극단값 (0.05% 이상)
        if abs(d["funding"]) > 0.0005:
            logger.info(f"[SIGNAL] 펀딩비 극단: {symbol} funding={d['funding']:.6f}")

        # Oracle-Mark 괴리 (0.5% 이상)
        if abs(d["divergence"]) > 0.005:
            logger.info(f"[SIGNAL] Oracle-Mark 괴리: {symbol} div={d['divergence']:.4%}")

    def get_top_funding(self, n: int = 5) -> list:
        """펀딩비 절댓값 상위 N개 심볼"""
        return sorted(
            self.market_data.values(),
            key=lambda x: abs(x["funding"]),
            reverse=True
        )[:n]

    def get_top_divergence(self, n: int = 5) -> list:
        """Oracle-Mark 괴리 상위 N개 심볼"""
        return sorted(
            self.market_data.values(),
            key=lambda x: abs(x["divergence"]),
            reverse=True
        )[:n]


# ── 테스트 ──────────────────────────────────
if __name__ == "__main__":
    import asyncio

    async def main():
        collector = DataCollector()

        async def stop_after(sec):
            await asyncio.sleep(sec)
            await collector.stop()

        asyncio.create_task(stop_after(8))
        await collector.start()

        print("\n=== 펀딩비 TOP 5 ===")
        for m in collector.get_top_funding():
            print(f"  {m['symbol']:10} funding={m['funding']:.6f} OI={m['open_interest']:.0f}")

        print("\n=== Oracle-Mark 괴리 TOP 5 ===")
        for m in collector.get_top_divergence():
            print(f"  {m['symbol']:10} divergence={m['divergence']:.4%} mark={m['mark']:.4f} oracle={m['oracle']:.4f}")

    asyncio.run(main())
