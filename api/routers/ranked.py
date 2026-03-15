"""
api/routers/ranked.py
CRS 기반 신뢰도 랭킹 라우터

GET  /traders/ranked           — CRS 신뢰도 점수 기반 트레이더 랭킹 (리얼타임)
GET  /traders/ranked/summary   — S/A/B/C 등급별 요약 통계
GET  /traders/ranked/{address} — 개별 트레이더 CRS 상세 분석
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from core.reliability import compute_crs, GRADE, MAX_COPY_RATIO

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/traders/ranked", tags=["ranked"])


def _leaderboard_row_to_crs(row: dict) -> dict:
    """DB/API leaderboard row → CRS 계산 → dict 반환"""
    try:
        result = compute_crs(row)
        d = result.to_dict()
        # 프론트 편의 필드 추가
        d["tier_label"] = _tier_label(result.grade)
        d["copy_ratio_pct"] = round(result.recommended_copy_ratio * 100, 1)
        return d
    except Exception as e:
        logger.warning(f"CRS 계산 오류 {row.get('address', '?')[:12]}: {e}")
        return {
            "address": row.get("address", ""),
            "alias": row.get("alias", ""),
            "crs": 0.0,
            "grade": "D",
            "disqualified": True,
            "disq_reason": f"계산 오류: {e}",
            "recommended_copy_ratio": 0.0,
            "copy_ratio_pct": 0.0,
            "tier_label": "❌ 제외",
            "warnings": [str(e)],
        }


def _tier_label(grade: str) -> str:
    labels = {
        "S": "🏆 Elite",
        "A": "⭐ Top",
        "B": "✅ Qualified",
        "C": "⚠️ Caution",
        "D": "❌ Excluded",
    }
    return labels.get(grade, "❓ Unknown")


async def _fetch_rows_from_db(limit: int = 200) -> list:
    """DB에서 active 트레이더 rows 가져오기"""
    try:
        from api.main import get_db
        db = await get_db()
        async with db.execute(
            "SELECT * FROM traders WHERE active=1 ORDER BY pnl_30d DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f"DB 조회 실패: {e}")
        return []


async def _fetch_rows_from_api(limit: int = 200) -> list:
    """Pacifica API에서 leaderboard 가져오기"""
    try:
        from pacifica.client import PacificaClient
        client = PacificaClient()
        return client.get_leaderboard(limit=limit) or []
    except Exception as e:
        logger.warning(f"Pacifica API 조회 실패: {e}")
        return []


@router.get("")
async def get_ranked_traders(
    limit: int = Query(20, ge=1, le=100),
    min_grade: str = Query("C", description="최소 등급 필터: S/A/B/C/D"),
    exclude_disqualified: bool = Query(True, description="하드 필터 제외 트레이더 숨김"),
):
    """
    CRS 신뢰도 점수 기반 트레이더 랭킹

    - 실시간 leaderboard 데이터 + CRS 알고리즘 적용
    - min_grade: 최소 등급 필터 (S/A/B/C/D), 기본 C 이상
    - exclude_disqualified: 하드 필터 제외 트레이더 숨김 (기본 true)
    """
    # DB 우선, 없으면 API
    rows = await _fetch_rows_from_db(200)
    source = "db"
    if not rows:
        rows = await _fetch_rows_from_api(200)
        source = "api"

    if not rows:
        return {"data": [], "count": 0, "source": "empty", "message": "트레이더 데이터 없음"}

    ranked = [_leaderboard_row_to_crs(r) for r in rows]

    # 필터링
    grade_order = {"S": 4, "A": 3, "B": 2, "C": 1, "D": 0}
    min_threshold = grade_order.get(min_grade.upper(), 0)
    filtered = []
    for t in ranked:
        if exclude_disqualified and t.get("disqualified"):
            continue
        grade = t.get("grade", "D")
        if grade_order.get(grade, 0) >= min_threshold:
            filtered.append(t)

    # CRS 점수 기준 내림차순 정렬
    filtered.sort(key=lambda x: x.get("crs", 0), reverse=True)

    return {
        "data": filtered[:limit],
        "count": len(filtered),
        "total_analyzed": len(ranked),
        "source": source,
    }


@router.get("/summary")
async def get_ranked_summary():
    """등급별 요약 통계"""
    rows = await _fetch_rows_from_db(300)
    if not rows:
        rows = await _fetch_rows_from_api(200)

    summary = {g: {"count": 0, "avg_crs": 0.0, "avg_roi_30d": 0.0, "traders": []} for g in ["S", "A", "B", "C", "D"]}

    for row in rows:
        crs_data = _leaderboard_row_to_crs(row)
        grade = crs_data.get("grade", "D")
        if grade not in summary:
            grade = "D"
        summary[grade]["count"] += 1
        summary[grade]["avg_crs"] += crs_data.get("crs", 0)
        summary[grade]["avg_roi_30d"] += (row.get("roi_30d") or 0)
        if grade in ["S", "A"] and len(summary[grade]["traders"]) < 5:
            summary[grade]["traders"].append({
                "address": crs_data["address"],
                "alias": crs_data.get("alias", ""),
                "crs": crs_data.get("crs", 0),
                "grade": grade,
                "tier_label": crs_data.get("tier_label", ""),
                "recommended_copy_ratio": crs_data.get("recommended_copy_ratio", 0),
            })

    for g in summary:
        n = summary[g]["count"]
        if n > 0:
            summary[g]["avg_crs"] = round(summary[g]["avg_crs"] / n, 1)
            summary[g]["avg_roi_30d"] = round(summary[g]["avg_roi_30d"] / n, 2)

    return {
        "total": len(rows),
        "summary": summary,
    }


@router.get("/{address}")
async def get_ranked_trader_detail(address: str):
    """개별 트레이더 CRS 상세 분석"""
    row = None

    # DB 우선
    try:
        from api.main import get_db
        db = await get_db()
        async with db.execute(
            "SELECT * FROM traders WHERE address = ?", (address,)
        ) as cur:
            r = await cur.fetchone()
        if r:
            row = dict(r)
    except Exception as e:
        logger.warning(f"DB 조회 실패: {e}")

    # API fallback
    if not row:
        try:
            from pacifica.client import PacificaClient
            client = PacificaClient()
            account_data = client.get_account(address)
            if account_data:
                row = {**account_data, "address": address}
        except Exception:
            pass

    if not row:
        raise HTTPException(404, f"트레이더를 찾을 수 없습니다: {address[:12]}...")

    crs_data = _leaderboard_row_to_crs(row)

    # trades/history 추가 분석
    trades = []
    try:
        from pacifica.client import PacificaClient
        client = PacificaClient()
        trades = client.get_trades_history(address, limit=100) or []
    except Exception:
        pass

    if trades:
        from core.reliability import calc_trade_stats
        trade_stats = calc_trade_stats(trades)
        crs_data["trade_stats"] = trade_stats
        crs_data["trades_analyzed"] = len(trades)

    return {"data": crs_data, "source": "crs_detail"}
