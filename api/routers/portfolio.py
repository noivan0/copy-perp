"""
api/routers/portfolio.py
포트폴리오 최적 배분 라우터

GET /portfolio/optimal  - CRS 기반 최적 트레이더 포트폴리오
GET /portfolio/backtest - 간단 백테스트
"""
import logging
from fastapi import APIRouter, Query
from core.reliability import compute_crs

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/portfolio", tags=["portfolio"])

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
            except:
                pass
        return results
    except Exception as e:
        logger.warning(f"DB 조회 실패: {e}")
        return []

@router.get("/optimal")
async def get_optimal_portfolio(
    max_traders: int = Query(5, ge=2, le=10),
    min_grade: str = Query("B", description="최소 등급: S/A/B/C")
):
    """AutoResearch 결과 기반 최적 포트폴리오 배분"""
    grade_min_crs = {"S": 80, "A": 70, "B": 50, "C": 30}
    min_crs = grade_min_crs.get(min_grade.upper(), 50)

    qualified = await _get_qualified_traders(min_crs)
    if not qualified:
        return {"traders": [], "weights": {}, "message": "조건 충족 트레이더 없음"}

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

    return {
        "traders": traders_out,
        "weights": weights,
        "method": "crs_weighted",
        "expected_sharpe": round(expected_sharpe, 2),
        "total_traders_analyzed": len(qualified),
    }


@router.get("/backtest")
async def backtest_portfolio(
    traders: str = Query(..., description="콤마로 구분된 트레이더 주소"),
    copy_ratio: float = Query(0.1, ge=0.01, le=1.0),
):
    """지정 트레이더 30d PnL 기반 간단 백테스트"""
    addrs = [a.strip() for a in traders.split(",") if a.strip()]
    if not addrs:
        return {"error": "트레이더 주소를 입력하세요"}

    try:
        from api.main import get_db
        db = await get_db()
        results = []
        for addr in addrs:
            async with db.execute(
                "SELECT alias, pnl_30d, pnl_7d, pnl_1d, equity FROM traders WHERE address=?", (addr,)
            ) as cur:
                row = await cur.fetchone()
            if row:
                results.append(dict(row))
    except Exception as e:
        return {"error": str(e)}

    if not results:
        return {"error": "해당 트레이더를 DB에서 찾을 수 없음"}

    # 가중 평균 PnL (equal weight)
    n = len(results)
    total_pnl = sum((r.get("pnl_30d") or 0) * copy_ratio / n for r in results)
    pnl_7d = sum((r.get("pnl_7d") or 0) * copy_ratio / n for r in results)
    avg_equity = sum((r.get("equity") or 1) for r in results) / n

    win_traders = sum(1 for r in results if (r.get("pnl_30d") or 0) > 0)
    win_rate = round(win_traders / n * 100, 1)

    return {
        "traders_count": n,
        "copy_ratio": copy_ratio,
        "estimated_pnl_30d": round(total_pnl, 2),
        "estimated_pnl_7d": round(pnl_7d, 2),
        "win_rate_pct": win_rate,
        "avg_equity": round(avg_equity, 2),
        "note": "단순 PnL 비례 추정 (슬리피지/수수료 미포함)",
    }
