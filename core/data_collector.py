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
_last_poll_ts: float = 0.0
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
            existing = _price_cache.get(sym, {})
            _price_cache[sym] = {
                "symbol": sym,
                "mark":          str(item.get("mark", item.get("mark_price", "0"))),
                "oracle":        str(item.get("oracle", item.get("oracle_price", "0"))),
                "funding":       str(item.get("funding", item.get("funding_rate", "0"))),
                "open_interest": str(item.get("open_interest", item.get("oi", "0"))),
                "volume_24h":    str(item.get("volume_24h", "0")),
                "mid":           str(item.get("mid", "0")),
                "updated_at":    time.time(),
                # 마켓 메타(lot_size 등) — 별도 폴링에서 채워짐, 기존 값 보존
                "lot_size":      existing.get("lot_size", item.get("lot_size", "0")),
                "min_order_size": existing.get("min_order_size", item.get("min_order_size", "0")),
            }
            updated += 1

        _last_updated = time.time()
        _data_connected = True
        return updated

    except Exception as e:
        logger.warning(f"DataCollector 폴링 실패: {e}")
        _data_connected = False
        return 0


async def poll_leaderboard_snapshot(db=None):
    """
    리더보드 스냅샷 — equity_daily 테이블에 저장 (QA 권고 2026-03-16)
    매일 00:00 UTC 또는 첫 폴링 시 실행.
    7~14일 누적 후 실측 Sharpe 계산 가능.
    """
    try:
        import requests
        BASE = "https://do5jt23sqak4.cloudfront.net/api/v1"
        HDR  = {"User-Agent": "copyperp/1.0", "Host": "api.pacifica.fi"}
        r = requests.get(f"{BASE}/leaderboard?limit=100&sortBy=pnl_all_time",
                         headers=HDR, timeout=8)
        if not r.ok:
            return
        d = r.json()
        traders = d.get("data", []) if isinstance(d, dict) else []
        if db and traders:
            await db.snapshot_equity_daily(traders)
            logger.info(f"equity_daily 스냅샷: {len(traders)}명 저장")
        return traders
    except Exception as e:
        logger.warning(f"leaderboard 스냅샷 실패: {e}")
        return []


_last_snapshot_date: str = ""


async def poll_market_meta():
    """마켓 메타데이터(lot_size, min_order_size) 1회 로딩 → price_cache에 병합"""
    global _price_cache
    from pacifica.client import _cf_request
    try:
        result = _cf_request("GET", "info/markets")
        markets = result.get("data", result) if isinstance(result, dict) else result
        if not isinstance(markets, list):
            return
        for m in markets:
            sym = m.get("symbol")
            if not sym:
                continue
            if sym not in _price_cache:
                _price_cache[sym] = {"symbol": sym}
            _price_cache[sym]["lot_size"] = str(m.get("lot_size", "0"))
            _price_cache[sym]["min_order_size"] = str(m.get("min_order_size", "0"))
        logger.info(f"DataCollector: 마켓 메타 {len(markets)}개 로딩 완료")
    except Exception as e:
        logger.debug(f"마켓 메타 로딩 실패 (비필수): {e}")


async def start_polling(interval: int = POLL_INTERVAL):
    """비동기 폴링 루프 — asyncio.create_task()로 시작"""
    global _data_connected, _last_snapshot_date
    from datetime import datetime, timezone
    logger.info(f"DataCollector REST 폴링 시작 (interval={interval}s)")
    # 최초 1회 마켓 메타 로딩 (lot_size, min_order_size)
    await poll_market_meta()
    while True:
        try:
            n = await poll_once()
            if n > 0:
                logger.debug(f"DataCollector: {n}개 심볼 업데이트")
            else:
                _data_connected = False

            # 매일 UTC 00:00 ~ 00:05 사이 첫 폴링에서 leaderboard 스냅샷 저장
            # (Sharpe 정밀 계산용 equity_daily 테이블 누적)
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if today != _last_snapshot_date:
                await poll_leaderboard_snapshot(db=None)  # DB 인스턴스 없을 때 로그만
                _last_snapshot_date = today

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
