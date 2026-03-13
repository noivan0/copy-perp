"""
Data Collector — REST 폴링 기반 시장 데이터 수집
(WebSocket이 HMG 방화벽에 막혀 REST 폴링으로 대체)

수집 항목:
- 가격 (mark, oracle, mid)
- 펀딩비 (funding, next_funding)
- 오픈 인터레스트
- Oracle-Mark 괴리율 (시그널용)
"""

import asyncio
import json
import logging
import time
from typing import Optional

import aiosqlite

logger = logging.getLogger(__name__)

POLL_INTERVAL = 30  # 초 단위 폴링 주기


class DataCollector:
    """REST 폴링 기반 시장 데이터 수집"""

    def __init__(self, db: Optional[aiosqlite.Connection] = None):
        self.db = db
        self._running = False
        self.market_data: dict[str, dict] = {}
        self._connected = False

    async def start(self):
        self._running = True
        logger.info("DataCollector 시작 (REST 폴링 모드, 주기=%ds)", POLL_INTERVAL)
        while self._running:
            try:
                await self._poll_once()
                self._connected = True
                await asyncio.sleep(POLL_INTERVAL)
            except Exception as e:
                logger.error("폴링 오류: %s, %d초 후 재시도", e, POLL_INTERVAL)
                self._connected = False
                await asyncio.sleep(POLL_INTERVAL)

    async def stop(self):
        self._running = False

    @property
    def is_connected(self) -> bool:
        return self._connected and bool(self.market_data)

    async def _poll_once(self):
        """allorigins CORS 프록시로 가격 데이터 폴링"""
        from pacifica.client import _proxy_get
        result = _proxy_get("info/prices")

        items = result if isinstance(result, list) else result.get("data", [])
        if not isinstance(items, list):
            raise RuntimeError(f"가격 데이터 형식 오류: {type(items)}")

        for item in items:
            await self._process_tick(item)

        logger.debug("폴링 완료: %d개 심볼 업데이트", len(items))

    async def _process_tick(self, tick: dict):
        symbol = tick.get("symbol")
        if not symbol:
            return

        mark   = float(tick.get("mark", 0) or 0)
        oracle = float(tick.get("oracle", 0) or 0)
        funding = float(tick.get("funding", 0) or 0)
        oi     = float(tick.get("open_interest", 0) or 0)
        ts     = tick.get("timestamp", int(time.time() * 1000))

        divergence = ((mark - oracle) / oracle) if oracle > 0 else 0

        self.market_data[symbol] = {
            "symbol":       symbol,
            "mark":         mark,
            "oracle":       oracle,
            "mid":          float(tick.get("mid", 0) or 0),
            "funding":      funding,
            "next_funding": float(tick.get("next_funding", 0) or 0),
            "open_interest": oi,
            "volume_24h":   float(tick.get("volume_24h", 0) or 0),
            "divergence":   divergence,
            "timestamp":    ts,
        }

        await self._check_signals(symbol)

    async def _check_signals(self, symbol: str):
        d = self.market_data.get(symbol)
        if not d:
            return
        if abs(d["funding"]) > 0.0005:
            logger.info("[SIGNAL] 펀딩비 극단: %s funding=%.6f", symbol, d["funding"])
        if abs(d["divergence"]) > 0.005:
            logger.info("[SIGNAL] Oracle-Mark 괴리: %s div=%.4f%%", symbol, d["divergence"] * 100)

    def get_top_funding(self, n: int = 5) -> list:
        return sorted(self.market_data.values(), key=lambda x: abs(x["funding"]), reverse=True)[:n]

    def get_top_divergence(self, n: int = 5) -> list:
        return sorted(self.market_data.values(), key=lambda x: abs(x["divergence"]), reverse=True)[:n]

    def get_price(self, symbol: str) -> Optional[float]:
        return self.market_data.get(symbol, {}).get("mark")


# ── 테스트 ──────────────────────────────────
if __name__ == "__main__":
    import asyncio

    async def main():
        collector = DataCollector()

        async def stop_after(sec):
            await asyncio.sleep(sec)
            await collector.stop()

        asyncio.create_task(stop_after(35))
        await collector.start()

        print("\n=== 펀딩비 TOP 5 ===")
        for m in collector.get_top_funding():
            print(f"  {m['symbol']:10} funding={m['funding']:.6f}")

        print("\n=== Oracle-Mark 괴리 TOP 5 ===")
        for m in collector.get_top_divergence():
            print(f"  {m['symbol']:10} div={m['divergence']:.4%}")

    asyncio.run(main())
