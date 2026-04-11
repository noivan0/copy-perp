"""
api/routers/ranked.py
CRS 기반 신뢰도 랭킹 라우터

GET  /traders/ranked           — CRS 신뢰도 점수 기반 트레이더 랭킹 (리얼타임)
GET  /traders/ranked/summary   — S/A/B/C 등급별 요약 통계
GET  /traders/ranked/{address} — 개별 트레이더 CRS 상세 분석
"""
import logging
import time as _time_mod
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from core.reliability import compute_crs, GRADE, MAX_COPY_RATIO


from api.utils import get_client_ip as _get_client_ip

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/traders/ranked", tags=["ranked"])

# ── 60초 인메모리 캐시 ────────────────────────────────────────────────────────
_ranked_cache: dict = {}  # key: (limit, min_grade, exclude_disqualified) → (ts, payload)
_RANKED_CACHE_TTL = 60    # 초


def _leaderboard_row_to_crs(row: dict) -> dict:
    """DB/API leaderboard row → CRS 계산 → dict 반환"""
    try:
        # roi_30d가 0이고 equity + pnl_30d가 있으면 계산해서 채움
        r = dict(row)
        if not r.get("roi_30d") and r.get("equity") and r.get("pnl_30d"):
            try:
                equity = float(r["equity"])
                pnl = float(r["pnl_30d"])
                cost_basis = equity - pnl
                if cost_basis > 0:
                    r["roi_30d"] = pnl / cost_basis * 100
            except Exception as _e:
                logger.debug(f"roi_30d calc skipped: {_e}")
        result = compute_crs(r)
        d = result.to_dict()
        # 프론트 편의 필드 추가
        d["tier_label"] = _tier_label(result.grade)
        d["copy_ratio_pct"] = round(result.recommended_copy_ratio * 100, 1)
        # top-level 편의 필드 (raw에서 추출 — 프론트 직접 접근용)
        d["pnl_30d"] = result.raw.get("pnl_30d")
        d["pnl_7d"]  = result.raw.get("pnl_7d")
        d["roi_30d"] = result.raw.get("roi_30d")
        # equity, oi: row 우선 (DB 최신값), raw 폴백
        d["equity"]  = float(row.get("equity") or result.raw.get("equity") or 0)
        d["oi"]      = float(row.get("oi_current") or result.raw.get("oi") or 0)
        # trade_stats: trades/history 없을 때 raw 기반 간이 통계로 채움
        # (N+1 API 호출 없이 목록에서 기본 필드 제공)
        # trade_stats: DB row 직접 필드 우선, 없으면 raw 기반 간이 통계
        if not d.get("trade_stats"):
            raw_data  = result.raw or {}
            # DB row에서 직접 읽기 (leaderboard sync 시 저장된 값)
            win_rate_val  = row.get("win_rate")   # DB에서 직접
            win_count     = row.get("win_count")
            lose_count    = row.get("lose_count")
            total_trades  = row.get("total_trades") or (
                (int(win_count or 0) + int(lose_count or 0)) if win_count is not None else None
            )
            profit_factor = row.get("profit_factor")
            roi_val       = row.get("roi_30d") or raw_data.get("roi_30d")
            # win_rate: DB값 우선, 없으면 win/lose count로 계산
            if win_rate_val is None and win_count is not None and total_trades:
                try:
                    win_rate_val = round(int(win_count) / int(total_trades) * 100, 1)
                except (ZeroDivisionError, TypeError):
                    win_rate_val = None
            d["trade_stats"] = {
                "win_rate":          round(float(win_rate_val) * (100 if float(win_rate_val) <= 1.0 else 1), 1) if win_rate_val is not None else None,
                "trade_count":       int(total_trades) if total_trades is not None else None,
                "win_count":         int(win_count) if win_count is not None else None,
                "lose_count":        int(lose_count) if lose_count is not None else None,
                "profit_factor":     round(float(profit_factor), 2) if profit_factor else None,
                "roi_30d":           round(float(roi_val), 2) if roi_val else None,
                "consistency_score": int(raw_data.get("consistency", 0)),
                "data_source":       "db" if win_rate_val is not None else "summary",
            }
        return d
    except Exception as e:
        logger.warning(f"CRS 계산 오류 {row.get('address', '?')[:12]}: {e}")
        return {
            "address": row.get("address", ""),
            "alias": row.get("alias", ""),
            "crs": 0.0,
            "grade": "D",
            "disqualified": True,
            "disq_reason": f"Calculation error: {e}",
            "recommended_copy_ratio": 0.0,
            "copy_ratio_pct": 0.0,
            "tier_label": "❌ Disqualified",
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
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    min_grade: str = Query("C", description="최소 등급 필터: S/A/B/C/D"),
    exclude_disqualified: bool = Query(True, description="하드 필터 제외 트레이더 숨김"),
):
    """
    CRS 신뢰도 점수 기반 트레이더 랭킹

    - 실시간 leaderboard 데이터 + CRS 알고리즘 적용
    - min_grade: 최소 등급 필터 (S/A/B/C/D), 기본 C 이상
    - exclude_disqualified: 하드 필터 제외 트레이더 숨김 (기본 true)
    - Rate limit: IP당 분당 30회
    """
    from api.utils import check_rate_limit as _crl
    client_ip = _get_client_ip(request)
    if not _crl(f"ranked:{client_ip}", 30, 60):
        raise HTTPException(429, {"error": "Rate limit exceeded — please wait", "code": "RATE_LIMIT_EXCEEDED"})

    # ── 60초 캐시 확인 ───────────────────────────────────
    _cache_key = (limit, min_grade.upper(), exclude_disqualified)
    _cached = _ranked_cache.get(_cache_key)
    if _cached and (_time_mod.time() - _cached[0]) < _RANKED_CACHE_TTL:
        logger.debug(f"[ranked] 캐시 히트 (TTL {_RANKED_CACHE_TTL}s)")
        return {**_cached[1], "cached": True, "cache_age_sec": round(_time_mod.time() - _cached[0], 1)}

    # DB 우선, 없으면 API
    rows = await _fetch_rows_from_db(200)
    source = "db"
    if not rows:
        rows = await _fetch_rows_from_api(200)
        source = "api"

    if not rows:
        return {"data": [], "count": 0, "source": "empty", "message": "No trader data available"}

    ranked = [_leaderboard_row_to_crs(r) for r in rows]

    # 필터링
    grade_order = {"S": 4, "A": 3, "B": 2, "C": 1, "D": 0}
    min_grade_upper = min_grade.upper()
    if min_grade_upper not in grade_order:
        raise HTTPException(
            status_code=400,
            detail={"error": f"Invalid min_grade '{min_grade}'. Must be one of: S, A, B, C, D", "code": "INVALID_GRADE"}
        )
    min_threshold = grade_order.get(min_grade_upper, 0)
    filtered = []
    for t in ranked:
        if exclude_disqualified and t.get("disqualified"):
            continue
        grade = t.get("grade", "D")
        if grade_order.get(grade, 0) >= min_threshold:
            filtered.append(t)

    # CRS 점수 기준 내림차순 정렬
    filtered.sort(key=lambda x: x.get("crs", 0), reverse=True)

    result = {
        "data": filtered[:limit],
        "count": len(filtered),
        "total_analyzed": len(ranked),
        "source": source,
    }

    # 캐시 저장
    _ranked_cache[_cache_key] = (_time_mod.time(), result)

    return result


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

    total_analyzed = len(rows)
    return {
        "total": total_analyzed,
        "total_analyzed": total_analyzed,   # 테스트 호환 필드
        "summary": summary,
        "grade_thresholds": {               # 등급 기준 점수 공개
            "S": GRADE["S"],
            "A": GRADE["A"],
            "B": GRADE["B"],
            "C": GRADE["C"],
            "D": GRADE["D"],
        },
        "max_copy_ratio": MAX_COPY_RATIO,   # 등급별 최대 copy_ratio
    }


@router.post("/sync-mainnet")
async def sync_mainnet_traders(request: Request):
    """Mainnet 리더보드를 DB에 동기화 (IP당 분당 2회)"""
    from api.utils import check_rate_limit as _crl
    client_ip = _get_client_ip(request)
    if not _check_rate_limit(f"sync_mainnet:{client_ip}", max_calls=2, window_sec=60):
        raise HTTPException(429, "Too many requests")
    import os
    from pacifica.client import PacificaClient

    # mainnet 클라이언트
    saved_network = os.environ.get('NETWORK', 'testnet')
    os.environ['NETWORK'] = 'mainnet'
    try:
        client = PacificaClient()
        lb = client.get_leaderboard(limit=100) or []
    finally:
        os.environ['NETWORK'] = saved_network

    if not lb:
        return {"synced": 0, "error": "No mainnet data available"}

    from api.main import get_db
    db = await get_db()
    synced = 0

    for row in lb:
        addr = row.get('address', '')
        if not addr:
            continue
        alias = (row.get('username') or addr[:8])
        equity = float(row.get('equity_current') or 0)
        oi = float(row.get('oi_current') or 0)
        pnl_1d = float(row.get('pnl_1d') or 0)
        pnl_7d = float(row.get('pnl_7d') or 0)
        pnl_30d = float(row.get('pnl_30d') or 0)
        pnl_all = float(row.get('pnl_all_time') or 0)
        vol_30d = float(row.get('volume_30d') or 0)

        await db.execute("""
            INSERT INTO traders (address, alias, equity, oi, pnl_1d, pnl_7d, pnl_30d, pnl_all_time, volume_30d, active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(address) DO UPDATE SET
                alias=excluded.alias,
                equity=excluded.equity,
                oi=excluded.oi,
                pnl_1d=excluded.pnl_1d,
                pnl_7d=excluded.pnl_7d,
                pnl_30d=excluded.pnl_30d,
                pnl_all_time=excluded.pnl_all_time,
                volume_30d=excluded.volume_30d,
                last_synced=datetime('now')
        """, (addr, alias, equity, oi, pnl_1d, pnl_7d, pnl_30d, pnl_all, vol_30d))
        synced += 1

    await db.commit()

    # 상위 5명 CRS 계산
    top_rows = await _fetch_rows_from_db(5)
    top5 = [_leaderboard_row_to_crs(r) for r in top_rows[:5]]

    return {"synced": synced, "top5": [{"alias": t.get("alias"), "crs": t.get("crs"), "grade": t.get("grade")} for t in top5]}


@router.get("/{address}/crs-history")
async def get_crs_history(address: str, days: int = Query(30, ge=1, le=365)):
    """
    개별 트레이더 CRS 점수 변동 이력 조회 (R8 신규)
    하루 1회 저장되는 스냅샷 기반
    """
    try:
        from api.main import get_db
        from db.database import get_crs_history as _get_history
        db = await get_db()
        history = await _get_history(db, address, days)
    except Exception as e:
        raise HTTPException(500, {"error": f"History query failed: {e}"})

    if not history:
        return {
            "address": address,
            "history": [],
            "count": 0,
            "message": "No CRS history found (snapshots save daily after win_rate refresh)"
        }

    return {
        "address": address,
        "history": history,
        "count": len(history),
        "oldest": history[-1]["computed_at"] if history else None,
        "latest": history[0]["computed_at"] if history else None,
        "crs_delta": round(history[0]["crs"] - history[-1]["crs"], 1) if len(history) > 1 else 0,
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
        except Exception as e:
            logger.debug(f"무시된 예외: {e}")

    if not row:
        raise HTTPException(404, {"error": f"Trader not found: {address[:12]}...", "code": "NOT_FOUND"})

    crs_data = _leaderboard_row_to_crs(row)

    # trades/history 추가 분석
    trades = []
    try:
        from pacifica.client import PacificaClient
        client = PacificaClient()
        trades = client.get_trades_history(address, limit=100) or []
    except Exception as e:
        logger.debug(f"무시된 예외: {e}")

    if trades:
        from core.reliability import calc_trade_stats
        trade_stats = calc_trade_stats(trades)
        crs_data["trade_stats"] = trade_stats
        crs_data["trades_analyzed"] = len(trades)

    return {"data": crs_data, "source": "crs_detail"}
