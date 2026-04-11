"""
마켓 라우터
GET /markets/{symbol}     — 심볼별 상세

수정 이력:
- 2025-03-16: collector 전역변수 참조 버그 수정 → _get_pc() 사용으로 교체
- 2026-04-12: 목록/signals 라우트 제거 (main.py에 통합됨), /{symbol} 경로만 유지
              include_router 등록으로 /markets/BTC 경로 활성화
"""

from fastapi import APIRouter, HTTPException
from core.data_collector import get_price_cache as _get_pc

router = APIRouter(prefix="/markets", tags=["markets"])


@router.get("/{symbol}")
async def get_market(symbol: str):
    """심볼 상세 — /markets/BTC, /markets/ETH 등 path 파라미터 방식"""
    cache = _get_pc()
    sym = symbol.upper()

    if sym in cache:
        return {"data": cache[sym], "source": "live"}

    raise HTTPException(
        status_code=404,
        detail={"error": f"Symbol {sym} not found", "code": "NOT_FOUND"}
    )
