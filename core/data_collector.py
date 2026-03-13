"""
DataCollector — REST 폴링 기반 시장 데이터 수집기
WS(HMG 차단) 완전 대체. CloudFront SNI GET으로 30초 주기 폴링.

- GET /info/prices → _price_cache 유지
- `data_connected` 플래그 → health 엔드포인트용
- 나머지 코드 변경 없이 _price_cache dict 동일하게 유지
"""
import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

POLL_INTERVAL = 30          # 초 (기본 30초)
STALE_THRESHOLD = 90        # 초 이상 업데이트 없으면 disconnected 처리
RETRY_INTERVAL  = 5         # 오류 시 재시도 간격

_price_cache: dict = {}
_last_updated: float = 0.0
_data_connected: bool = False


def get_price_cache() -> dict:
    return _price_cache


def is_connected() -> bool:
    return _data_connected and (time.time() - _last_updated) < STALE_THRESHOLD


async def poll_once() -> int:
    """가격 데이터 1회 폴링 → _price_cache 업데이트. 업데이트된 심볼 수 반환."""
    global _price_cache, _last_updated, _data_connected
    from pacifica.client import _cf_request
    try:
        result = _cf_request("GET", "info/prices")
        prices = result.get("data", result) if isinstance(result, dict) else result
        if not isinstance(prices, list) or not prices:
            logger.debug("prices 빈 응답")
            return 0

        updated = 0
        for item in prices:
            sym = item.get("symbol")
            if not sym:
                continue
            _price_cache[sym] = {
                "symbol": sym,
                "mark":          str(item.get("mark", item.get("mark_price", "0"))),
                "oracle":        str(item.get("oracle", item.get("oracle_price", "0"))),
                "funding":       str(item.get("funding", item.get("funding_rate", "0"))),
                "open_interest": str(item.get("open_interest", item.get("oi", "0"))),
                "volume_24h":    str(item.get("volume_24h", "0")),
                "mid":           str(item.get("mid", "0")),
                "updated_at":    time.time(),
            }
            updated += 1

        _last_updated = time.time()
        _data_connected = True
        return updated

    except Exception as e:
        logger.warning(f"DataCollector 폴링 실패: {e}")
        _data_connected = False
        return 0


async def start_polling(interval: int = POLL_INTERVAL):
    """비동기 폴링 루프 — asyncio.create_task()로 시작"""
    global _data_connected
    logger.info(f"DataCollector REST 폴링 시작 (interval={interval}s)")
    while True:
        try:
            n = await poll_once()
            if n > 0:
                logger.debug(f"DataCollector: {n}개 심볼 업데이트")
            else:
                _data_connected = False
        except Exception as e:
            logger.error(f"DataCollector 루프 오류: {e}")
            _data_connected = False
            await asyncio.sleep(RETRY_INTERVAL)
            continue
        await asyncio.sleep(interval)


# ── 동기 단발 조회 (테스트용) ─────────────────────────────
if __name__ == "__main__":
    import sys; sys.path.insert(0, ".")
    from dotenv import load_dotenv; load_dotenv(".env")

    async def test():
        print("DataCollector 테스트 (1회 폴링)...")
        n = await poll_once()
        print(f"업데이트: {n}개 심볼")
        btc = _price_cache.get("BTC", {})
        print(f"BTC mark={btc.get('mark')} funding={btc.get('funding')}")
        print(f"data_connected={is_connected()}")

    asyncio.run(test())
