"""
api/routers/tracker.py — 메인넷 장기 PnL 추적 API

GET /tracker/report           최신 누적 리포트 (JSON)
GET /tracker/snapshots        트레이더 스냅샷 이력 (최근 N개)
GET /tracker/sim-pnl          시나리오별 시뮬 PnL 이력
GET /tracker/trust-metrics    신뢰도 기준 달성 이력
"""

import os
import sqlite3
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/tracker", tags=["tracker"])

# mainnet_tracker.db 경로
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_DB = os.path.join(_BASE_DIR, "mainnet_tracker.db")


def _get_conn(db_path: str = DEFAULT_DB) -> Optional[sqlite3.Connection]:
    """DB 연결 반환. 파일 없으면 None."""
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _rows_to_list(rows) -> list:
    return [dict(r) for r in rows]


# ── GET /tracker/report ──────────────────────────────────────────
@router.get("/report")
def get_tracker_report(days: int = Query(30, ge=1, le=365)):
    """
    누적 데이터 기반 장기 리포트 (JSON)
    - 트레이더별 ROI 추이
    - 팔로워 시나리오별 누적 PnL
    - 신뢰도 기준 달성 현황
    """
    try:
        from core.mainnet_tracker import get_accumulated_report, DEFAULT_DB_PATH
        db_path = os.environ.get("MAINNET_DB_PATH", DEFAULT_DB_PATH)
        report = get_accumulated_report(db_path=db_path, days=days)
        return {"ok": True, "data": report, "days": days}
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": str(e), "code": "TRACKER_ERROR"})


# ── GET /tracker/snapshots ───────────────────────────────────────
@router.get("/snapshots")
def get_snapshots(
    limit: int = Query(50, ge=1, le=500),
    address: Optional[str] = Query(None),
    days: int = Query(30, ge=1, le=365),
):
    """
    트레이더 스냅샷 이력 조회
    - limit: 최근 N개
    - address: 특정 트레이더 필터
    - days: 기간 필터
    """
    from core.mainnet_tracker import DEFAULT_DB_PATH

    db_path = os.environ.get("MAINNET_DB_PATH", DEFAULT_DB_PATH)
    conn = _get_conn(db_path)
    if not conn:
        return {"ok": True, "data": [], "count": 0, "note": "DB 없음. 수집 후 재조회."}

    since = int(time.time()) - days * 86400
    try:
        if address:
            rows = conn.execute(
                "SELECT * FROM trader_snapshots WHERE address=? AND collected_at >= ? ORDER BY collected_at DESC LIMIT ?",
                (address, since, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trader_snapshots WHERE collected_at >= ? ORDER BY collected_at DESC LIMIT ?",
                (since, limit),
            ).fetchall()
        data = _rows_to_list(rows)
        conn.close()
        return {"ok": True, "data": data, "count": len(data)}
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail={"error": str(e), "code": "DB_ERROR"})


# ── GET /tracker/sim-pnl ─────────────────────────────────────────
@router.get("/sim-pnl")
def get_sim_pnl(
    limit: int = Query(50, ge=1, le=500),
    scenario: Optional[str] = Query(None),
    days: int = Query(30, ge=1, le=365),
):
    """
    시나리오별 팔로워 시뮬 PnL 이력
    - scenario: conservative_1k / balanced_5k / aggressive_10k / full_10k
    - limit: 최근 N개
    """
    from core.mainnet_tracker import DEFAULT_DB_PATH

    db_path = os.environ.get("MAINNET_DB_PATH", DEFAULT_DB_PATH)
    conn = _get_conn(db_path)
    if not conn:
        return {"ok": True, "data": [], "count": 0, "note": "DB 없음. 수집 후 재조회."}

    since = int(time.time()) - days * 86400
    try:
        if scenario:
            rows = conn.execute(
                "SELECT * FROM follower_sim_pnl WHERE scenario=? AND collected_at >= ? ORDER BY collected_at DESC LIMIT ?",
                (scenario, since, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM follower_sim_pnl WHERE collected_at >= ? ORDER BY collected_at DESC LIMIT ?",
                (since, limit),
            ).fetchall()
        data = _rows_to_list(rows)
        conn.close()

        # 시나리오별 요약
        summary: dict = {}
        for row in data:
            sc = row.get("scenario", "")
            if sc not in summary:
                summary[sc] = {
                    "scenario": sc,
                    "latest_roi_pct": row.get("roi_pct", 0),
                    "latest_pnl": row.get("pnl_cumulative", 0),
                    "count": 0,
                }
            summary[sc]["count"] += 1

        return {
            "ok": True,
            "data": data,
            "count": len(data),
            "summary": list(summary.values()),
        }
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail={"error": str(e), "code": "DB_ERROR"})


# ── GET /tracker/trust-metrics ───────────────────────────────────
@router.get("/trust-metrics")
def get_trust_metrics(
    limit: int = Query(30, ge=1, le=200),
    days: int = Query(30, ge=1, le=365),
):
    """
    신뢰도 기준 달성 이력
    - L2 트레이더 기준 (ROI≥10%, ROI≥30%, avgCRS)
    - L3 사용자 PnL 기준 (시뮬 ROI)
    - 달성 여부 (l2_met, l3_target_met)
    """
    from core.mainnet_tracker import DEFAULT_DB_PATH

    db_path = os.environ.get("MAINNET_DB_PATH", DEFAULT_DB_PATH)
    conn = _get_conn(db_path)
    if not conn:
        return {"ok": True, "data": [], "count": 0, "note": "DB 없음. 수집 후 재조회."}

    since = int(time.time()) - days * 86400
    try:
        rows = conn.execute(
            "SELECT * FROM trust_metrics WHERE collected_at >= ? ORDER BY collected_at DESC LIMIT ?",
            (since, limit),
        ).fetchall()
        data = _rows_to_list(rows)
        conn.close()

        if data:
            latest = data[0]
            l2_rate = sum(1 for r in data if r.get("l2_met")) / len(data) * 100
            l3_rate = sum(1 for r in data if r.get("l3_target_met")) / len(data) * 100
            summary = {
                "latest_l2_met": bool(latest.get("l2_met")),
                "latest_l3_met": bool(latest.get("l3_target_met")),
                "latest_avg_crs": latest.get("avg_crs", 0),
                "latest_sim_30d_roi_1k": latest.get("sim_30d_roi_1k", 0),
                "l2_achievement_rate_pct": round(l2_rate, 1),
                "l3_achievement_rate_pct": round(l3_rate, 1),
                "total_checks": len(data),
            }
        else:
            summary = {}

        return {
            "ok": True,
            "data": data,
            "count": len(data),
            "summary": summary,
        }
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail={"error": str(e), "code": "DB_ERROR"})
