"""
마켓 라우터
GET /markets              — 전체 마켓 (가격 캐시 + Mock 폴백)
GET /markets/{symbol}     — 심볼별 상세
GET /markets/signals      — 현재 시그널 TOP (펀딩비, Oracle-Mark 괴리)

수정 이력:
- 2025-03-16: collector 전역변수 참조 버그 수정 → _get_pc() 사용으로 교체
              (api/main.py에는 collector 없음, core/data_collector.get_price_cache() 사용)
"""

from fastapi import APIRouter
from core.mock import MOCK_MARKET_DATA
from core.data_collector import get_price_cache as _get_pc, is_connected as _dc_connected

router = APIRouter(prefix="/markets", tags=["markets"])


@router.get("")
async def list_markets():
    """전체 마켓 데이터 — REST 폴링 캐시 우선, Mock 폴백"""
    cache = _get_pc()
    if cache:
        return {"data": list(cache.values()), "source": "live", "count": len(cache)}
    return {"data": list(MOCK_MARKET_DATA.values()), "source": "mock", "count": len(MOCK_MARKET_DATA)}


@router.get("/signals")
async def get_signals(top_n: int = 5):
    """현재 시그널 — 펀딩비 극단 + Oracle-Mark 괴리"""
    cache = _get_pc()

    if cache:
        items = list(cache.values())
        funding_signals = sorted(items, key=lambda x: abs(float(x.get("funding", 0))), reverse=True)[:top_n]
        divergence_signals = sorted(
            [m for m in items if float(m.get("oracle", 0)) > 0],
            key=lambda x: abs(float(x.get("mark", 0)) - float(x.get("oracle", 0))) / max(float(x.get("oracle", 1)), 0.0001),
            reverse=True
        )[:top_n]
        source = "live"
    else:
        # Mock 시그널 폴백
        items = list(MOCK_MARKET_DATA.values())
        funding_signals = sorted(items, key=lambda x: abs(x.get("funding", 0)), reverse=True)[:top_n]
        divergence_signals = []
        source = "mock"

    return {
        "funding_extremes": funding_signals,
        "oracle_mark_divergence": divergence_signals,
        "source": source,
    }


@router.get("/{symbol}")
async def get_market(symbol: str):
    """심볼 상세"""
    cache = _get_pc()
    sym = symbol.upper()

    if sym in cache:
        return {"data": cache[sym], "source": "live"}

    if sym in MOCK_MARKET_DATA:
        return {"data": MOCK_MARKET_DATA[sym], "source": "mock"}

    return {"data": None, "error": f"Symbol {sym} not found"}
