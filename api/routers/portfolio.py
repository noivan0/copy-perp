"""
api/routers/portfolio.py
포트폴리오 최적 배분 라우터

GET /portfolio/optimal       - CRS 기반 최적 트레이더 포트폴리오
GET /portfolio/backtest      - 간단 백테스트
GET /portfolio/performance   - 팔로워 PnL 리포트
GET /portfolio/equity-curve  - equity curve 조회
"""
import logging
import re
from typing import Optional
from fastapi import APIRouter, Query, HTTPException, Request
from core.reliability import compute_crs

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/portfolio", tags=["portfolio"])

import time as _portfolio_time

_portfolio_cache: dict = {}  # key -> (ts, data)
_PORTFOLIO_TTL = 30.0  # 30초 캐시



_SOLANA_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

def _is_valid_solana_address(address: str) -> bool:
    return bool(_SOLANA_RE.match(address))

async def _get_qualified_traders(min_crs: float = 50.0) -> list:
    """CRS B등급 이상 트레이더 조회"""
    try:
        from api.main import get_db
        db = await get_db()
        async with db.execute(
            "SELECT * FROM traders WHERE active=1 ORDER BY pnl_30d DESC LIMIT 100"
        ) as cur:
            rows = await cur.fetchall()
        results = []
        for row in rows:
            d = dict(row)
            try:
                crs = compute_crs(d)
                if crs.crs >= min_crs and not crs.disqualified:
                    results.append({"row": d, "crs": crs})
            except Exception as e:
                logger.debug(f"무시된 예외: {e}")
        return results
    except Exception as e:
        logger.warning(f"DB 조회 실패: {e}")
        return []

@router.get("/optimal")
async def get_optimal_portfolio(
    request: Request,
    max_traders: int = Query(5, ge=2, le=10),
    min_grade: str = Query("B", description="최소 등급: S/A/B/C")
):
    """AutoResearch 결과 기반 최적 포트폴리오 배분"""
    # BUG-R2-2 수정: min_grade 유효값 검증 (422 반환)
    grade_min_crs = {"S": 80, "A": 70, "B": 50, "C": 30}
    if min_grade.upper() not in grade_min_crs:
        raise HTTPException(
            status_code=422,
            detail={"error": f"Invalid min_grade '{min_grade}'. Must be one of: S, A, B, C", "code": "INVALID_PARAM"}
        )
    min_crs = grade_min_crs[min_grade.upper()]

    # 캐시 확인 (30초 TTL)
    _cache_key = f"optimal:{max_traders}:{min_grade.upper()}"
    _now = _portfolio_time.time()
    if _cache_key in _portfolio_cache:
        _ts, _data = _portfolio_cache[_cache_key]
        if _now - _ts < _PORTFOLIO_TTL:
            return {**_data, "cached": True}

    qualified = await _get_qualified_traders(min_crs)
    if not qualified:
        return {"traders": [], "weights": {}, "message": "No traders meeting criteria"}

    # Sharpe(모멘텀) 기준 상위 N명 선별
    qualified.sort(key=lambda x: x["crs"].crs, reverse=True)
    selected = qualified[:max_traders]

    # 추천 비중 정규화
    total_ratio = sum(x["crs"].recommended_copy_ratio for x in selected) or 1
    traders_out = []
    weights = {}

    for item in selected:
        crs = item["crs"]
        row = item["row"]
        norm_weight = round(crs.recommended_copy_ratio / total_ratio, 3)
        addr = crs.address
        weights[addr] = norm_weight
        traders_out.append({
            "address": addr,
            "alias": row.get("alias", addr[:8]),
            "crs": round(crs.crs, 1),
            "grade": crs.grade,
            "tier_label": "🏆 Elite" if crs.grade=="S" else "⭐ Top" if crs.grade=="A" else "✅ Qualified" if crs.grade=="B" else "⚠️ Caution",
            "weight_pct": round(norm_weight * 100, 1),
            "recommended_copy_ratio": crs.recommended_copy_ratio,
            "pnl_30d": row.get("pnl_30d", 0),
            "pnl_7d": row.get("pnl_7d", 0),
        })

    # 예상 가중 Sharpe (CRS 기반 근사)
    expected_sharpe = sum(t["crs"] * t["weight_pct"] / 100 for t in traders_out) / 10

    _result = {
        "traders": traders_out,
        "weights": weights,
        "method": "crs_weighted",
        "expected_sharpe": round(expected_sharpe, 2),
        "total_traders_analyzed": len(qualified),
        "cached": False,
    }
    _portfolio_cache[_cache_key] = (_portfolio_time.time(), _result)
    return _result


@router.get("/backtest")
async def backtest_portfolio(
    traders: str = Query(None, description="콤마로 구분된 트레이더 주소 (없으면 최적 포트폴리오 자동 사용)"),
    copy_ratio: float = Query(0.1, ge=0.01, le=1.0),
):
    """지정 트레이더 30d PnL 기반 간단 백테스트. traders 미지정 시 최적 포트폴리오 자동 사용."""
    # traders 미지정 시 DB에서 CRS 상위 5명 자동 선택
    if not traders:
        qualified = await _get_qualified_traders(min_crs=50.0)
        qualified.sort(key=lambda x: x["crs"].crs, reverse=True)
        addrs = [x["crs"].address for x in qualified[:5]]
        if not addrs:
            return {"error": "No traders meet the criteria. Specify traders parameter directly."}
    else:
        addrs = [a.strip() for a in traders.split(",") if a.strip()]
    if not addrs:
        return {"error": "Please provide a trader address"}

    try:
        from api.main import get_db
        db = await get_db()
        results = []
        for addr in addrs:
            async with db.execute(
                "SELECT address, alias, pnl_30d, pnl_7d, pnl_1d, equity FROM traders WHERE address=?", (addr,)
            ) as cur:
                row = await cur.fetchone()
            if row:
                results.append(dict(row))
    except Exception as e:
        return {"error": str(e)}

    if not results:
        return {"error": "Trader not found in DB"}

    # 가중 평균 PnL (equal weight)
    n = len(results)
    total_pnl = sum((r.get("pnl_30d") or 0) * copy_ratio / n for r in results)
    pnl_7d = sum((r.get("pnl_7d") or 0) * copy_ratio / n for r in results)
    avg_equity = sum((r.get("equity") or 1) for r in results) / n

    win_traders = sum(1 for r in results if (r.get("pnl_30d") or 0) > 0)
    win_rate = round(win_traders / n * 100, 1)

    response = {
        "traders_count": n,
        "copy_ratio": copy_ratio,
        "estimated_pnl_30d": round(total_pnl, 2),
        "estimated_pnl_7d": round(pnl_7d, 2),
        "win_rate_pct": win_rate,
        "avg_equity": round(avg_equity, 2),
        "note": "Simple PnL estimate (slippage/fees not included)",
    }
    # portfolio key for backward compatibility
    response["portfolio"] = [{"address": r.get("address"), "pnl_30d": r.get("pnl_30d"), "equity": r.get("equity")} for r in results]
    return response


@router.get("/performance")
async def get_follower_performance(
    follower_address: Optional[str] = Query(None, description="팔로워 주소 (follower_address)"),
    address: Optional[str] = Query(None, description="팔로워 주소 (address — 하위호환)"),
    days: int = Query(30, ge=1, le=365),
):
    """팔로워 PnL 리포트 — Sharpe, MDD, by_trader/symbol, daily equity curve"""
    addr = follower_address or address  # follower_address 우선, 하위호환 address 폴백
    if not addr or not _is_valid_solana_address(addr):
        raise HTTPException(status_code=400, detail={"error": "Invalid Solana address", "code": "INVALID_ADDRESS"})
    try:
        from api.main import get_db
        from core.stats import compute_follower_pnl_report
        db = await get_db()
        return await compute_follower_pnl_report(db, addr, days)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"performance 조회 실패 {addr[:12]}: {e}")
        raise HTTPException(status_code=500, detail={"error": "Internal server error", "code": "INTERNAL_SERVER_ERROR"})


@router.get("/equity-curve")
async def get_equity_curve(
    follower_address: Optional[str] = Query(None, description="팔로워 주소 (follower_address)"),
    address: Optional[str] = Query(None, description="팔로워 주소 (address — 하위호환)"),
    days: int = Query(30, ge=1, le=365),
):
    """Equity Curve 조회 — snapshot 우선, 없으면 실시간 계산"""
    address = follower_address or address  # follower_address 우선
    if not address or not _is_valid_solana_address(address):
        raise HTTPException(status_code=400, detail={"error": "Invalid Solana address", "code": "INVALID_ADDRESS"})
    try:
        from api.main import get_db
        from core.stats import compute_follower_pnl_report
        import time as _time
        db = await get_db()

        # performance_snapshots 테이블에서 먼저 조회
        cutoff_date = __import__("datetime").datetime.utcfromtimestamp(
            _time.time() - days * 86400
        ).strftime("%Y-%m-%d")
        async with db.execute(
            """SELECT snapshot_date, equity, daily_pnl, cum_roi_pct
               FROM performance_snapshots
               WHERE address = ? AND snapshot_date >= ?
               ORDER BY snapshot_date ASC""",
            (address, cutoff_date)
        ) as cur:
            rows = await cur.fetchall()

        if rows:
            data = [
                {
                    "date": r["snapshot_date"],
                    "equity": r["equity"],
                    "daily_pnl": r["daily_pnl"],
                    "cum_roi_pct": r["cum_roi_pct"],
                }
                for r in rows
            ]
            return {"address": address, "days": days, "data": data, "source": "snapshot"}

        # snapshot 없으면 실시간 계산
        report = await compute_follower_pnl_report(db, address, days)
        return {
            "address": address,
            "days": days,
            "data": report.get("daily_equity", []),
            "source": "realtime",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"equity-curve 조회 실패 {address[:12]}: {e}")
        raise HTTPException(status_code=500, detail={"error": "Internal server error", "code": "INTERNAL_SERVER_ERROR"})
