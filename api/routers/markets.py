"""
마켓 라우터
GET /markets              — 전체 마켓 (Mock + 실시간 혼합)
GET /markets/{symbol}     — 심볼별 상세
GET /markets/signals      — 현재 시그널 TOP (펀딩비, Oracle-Mark 괴리)
"""

from fastapi import APIRouter
from core.mock import MOCK_MARKET_DATA

router = APIRouter(prefix="/markets", tags=["markets"])


@router.get("")
async def list_markets():
    """전체 마켓 데이터"""
    from api.main import collector
    if collector and collector.market_data:
        return {"data": list(collector.market_data.values()), "source": "live"}
    return {"data": list(MOCK_MARKET_DATA.values()), "source": "mock"}


@router.get("/signals")
async def get_signals(top_n: int = 5):
    """현재 시그널 — 펀딩비 극단 + Oracle-Mark 괴리"""
    from api.main import collector

    if collector and collector.market_data:
        funding_signals = collector.get_top_funding(top_n)
        divergence_signals = collector.get_top_divergence(top_n)
        source = "live"
    else:
        # Mock 시그널
        items = list(MOCK_MARKET_DATA.values())
        funding_signals = sorted(items, key=lambda x: abs(x["funding"]), reverse=True)[:top_n]
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
    from api.main import collector
    sym = symbol.upper()

    if collector and sym in collector.market_data:
        return {"data": collector.market_data[sym], "source": "live"}

    if sym in MOCK_MARKET_DATA:
        return {"data": MOCK_MARKET_DATA[sym], "source": "mock"}

    return {"data": None, "error": f"Symbol {sym} not found"}
