"""
api/routers/performance.py — 팔로워 실적 기록 API (PnL Tracker 완전 연동)

엔드포인트:
  GET  /performance/{follower}              — 팔로워 종합 성과 (실현+미실현 PnL)
  GET  /performance/{follower}/positions    — 현재 열린 포지션
  GET  /performance/{follower}/pnl          — 실현 PnL 이력
  GET  /performance/{follower}/equity       — 자산 추이 차트
  GET  /performance/{follower}/daily        — 일별 집계 (Sharpe, MDD)
  POST /performance/{follower}/snapshot     — 수동 스냅샷
  GET  /performance/ranking                 — 플랫폼 팔로워 랭킹
  GET  /performance/platform/stats          — 플랫폼 전체 통계
"""

import logging
import time
from datetime import datetime
from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/performance", tags=["performance"])


# ── 헬퍼 ──────────────────────────────────────────────────────────────────────

async def _get_db():
    from api.main import get_db
    return await get_db()


def _validate_addr(address: str) -> None:
    if not address or len(address) < 10:
        raise HTTPException(422, detail=f"Invalid address: {address!r}")


def _mask(addr: str) -> str:
    return addr[:6] + "..." + addr[-4:] if len(addr) > 10 else addr


# ── 스키마 ─────────────────────────────────────────────────────────────────────

class SnapshotRequest(BaseModel):
    base_capital: float = 10_000.0


# ── 엔드포인트 ─────────────────────────────────────────────────────────────────

@router.get("/ranking")
async def get_follower_ranking(limit: int = Query(20, ge=1, le=100)):
    """
    플랫폼 팔로워 ROI 랭킹.
    실현 PnL 기준, 주소 마스킹 처리.
    """
    db = await _get_db()
    try:
        async with db.execute(
            """
            SELECT pr.follower_address,
                   COUNT(*) AS total_trades,
                   SUM(CASE WHEN pr.net_pnl>0 THEN 1 ELSE 0 END) AS wins,
                   ROUND(SUM(pr.net_pnl),4) AS total_net_pnl,
                   ROUND(AVG(pr.roi_pct),2) AS avg_roi_pct,
                   ROUND(SUM(pr.fee_usdc+pr.builder_fee_usdc),4) AS total_fee
            FROM pnl_records pr
            GROUP BY pr.follower_address
            ORDER BY total_net_pnl DESC
            LIMIT ?
            """,
            (limit,)
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

        ranking = []
        for i, r in enumerate(rows, 1):
            total = r["total_trades"]
            wins  = r["wins"] or 0
            ranking.append({
                "rank": i,
                "address": _mask(r["follower_address"]),
                "total_net_pnl": r["total_net_pnl"],
                "avg_roi_pct": r["avg_roi_pct"],
                "total_trades": total,
                "win_rate_pct": round(wins / total * 100, 1) if total else 0,
                "total_fee_paid": r["total_fee"],
            })
        return {"ranking": ranking, "count": len(ranking), "note": "Address masked for privacy"}
    except Exception as e:
        logger.exception("ranking 오류")
        raise HTTPException(500, detail=str(e))


@router.get("/platform/stats")
async def get_platform_performance_stats():
    """플랫폼 전체 성과 통계"""
    db = await _get_db()
    try:
        # 전체 거래 통계
        async with db.execute(
            """
            SELECT COUNT(*) total, COUNT(DISTINCT follower_address) followers,
                   ROUND(SUM(net_pnl),4) total_pnl,
                   ROUND(AVG(net_pnl),4) avg_pnl,
                   ROUND(AVG(roi_pct),2) avg_roi,
                   SUM(CASE WHEN net_pnl>0 THEN 1 ELSE 0 END) wins
            FROM pnl_records
            """
        ) as cur:
            stats = dict(await cur.fetchone())

        # Builder Fee 누적
        async with db.execute("SELECT ROUND(SUM(fee_usdc),4), COUNT(*) FROM fee_records") as cur:
            fee_row = await cur.fetchone()

        # 열린 포지션
        async with db.execute(
            "SELECT COUNT(*), COUNT(DISTINCT follower_address) FROM positions WHERE status='open'"
        ) as cur:
            pos_row = await cur.fetchone()

        total = stats["total"] or 0
        wins  = stats["wins"] or 0
        return {
            "total_closed_trades": total,
            "unique_followers": stats["followers"] or 0,
            "total_realized_pnl": stats["total_pnl"] or 0,
            "avg_pnl_per_trade": stats["avg_pnl"] or 0,
            "avg_roi_pct": stats["avg_roi"] or 0,
            "platform_win_rate_pct": round(wins / total * 100, 1) if total else 0,
            "total_builder_fee": fee_row[0] or 0,
            "fee_count": fee_row[1] or 0,
            "open_positions": pos_row[0] or 0,
            "active_followers_with_positions": pos_row[1] or 0,
        }
    except Exception as e:
        logger.exception("platform stats 오류")
        raise HTTPException(500, detail=str(e))


@router.get("/{follower_address}")
async def get_performance_report(
    follower_address: str,
    days: int = Query(30, ge=1, le=365),
):
    """
    팔로워 종합 성과 리포트.
    실현 PnL + 미실현 PnL + Sharpe + MDD + 뱃지.
    """
    _validate_addr(follower_address)
    db = await _get_db()
    try:
        from db.pnl_tracker import get_performance_summary
        summary = await get_performance_summary(db, follower_address)

        # 뱃지 결정
        roi = summary.get("realized_pnl", 0)
        wr  = summary.get("win_rate_pct", 0)
        if roi > 0 and wr >= 60:
            badge, badge_msg = "🏆", "Elite Copier"
        elif roi > 0 and wr >= 50:
            badge, badge_msg = "⭐", "Top Performer"
        elif roi > 0:
            badge, badge_msg = "✅", "Profitable"
        else:
            badge, badge_msg = "🌱", "Growing"

        return {
            "follower_address": follower_address,
            "badge": badge,
            "badge_label": badge_msg,
            "summary": summary,
            "message": (
                f"Last {days} days: {summary['total_trades']} trades, "
                f"Realized PnL ${summary['realized_pnl']:+,.2f}, "
                f"Win rate {summary['win_rate_pct']:.1f}%, "
                f"Sharpe {summary['sharpe_30d']:.2f}"
            ),
        }
    except Exception as e:
        logger.exception(f"performance report 오류: {follower_address}")
        raise HTTPException(500, detail=str(e))


@router.get("/{follower_address}/positions")
async def get_open_positions(follower_address: str):
    """현재 열린 포지션 (미실현 PnL 포함)"""
    _validate_addr(follower_address)
    db = await _get_db()
    try:
        async with db.execute(
            """
            SELECT symbol, side, size, avg_entry_price, mark_price,
                   unrealized_pnl, opened_at, last_updated
            FROM positions
            WHERE follower_address=? AND status='open'
            ORDER BY opened_at DESC
            """,
            (follower_address,)
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

        for r in rows:
            r["direction"] = "LONG" if r["side"] == "bid" else "SHORT"
            r["opened_at_kst"] = datetime.fromtimestamp(r["opened_at"] / 1000).strftime("%Y-%m-%d %H:%M:%S")
            r["hold_min"] = round((time.time() * 1000 - r["opened_at"]) / 60000, 1)
            r["unrealized_pnl"] = round(r["unrealized_pnl"] or 0, 4)
            r["roi_pct"] = (
                round(r["unrealized_pnl"] / (r["avg_entry_price"] * r["size"]) * 100, 2)
                if r["avg_entry_price"] and r["size"] else 0
            )

        total_unrealized = sum(r["unrealized_pnl"] for r in rows)
        return {
            "follower_address": follower_address,
            "open_count": len(rows),
            "total_unrealized_pnl": round(total_unrealized, 4),
            "positions": rows,
        }
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.get("/{follower_address}/pnl")
async def get_pnl_history(
    follower_address: str,
    limit: int = Query(50, ge=1, le=500),
    symbol: str = Query(None),
):
    """실현 PnL 이력 (open/close 페어링 완료된 거래만)"""
    _validate_addr(follower_address)
    db = await _get_db()
    try:
        from db.pnl_tracker import get_pnl_history
        records = await get_pnl_history(db, follower_address, limit)

        if symbol:
            records = [r for r in records if r["symbol"].upper() == symbol.upper()]

        # 요약
        wins = sum(1 for r in records if r["net_pnl"] > 0)
        total_net = sum(r["net_pnl"] for r in records)
        gross_profit = sum(r["net_pnl"] for r in records if r["net_pnl"] > 0)
        gross_loss   = abs(sum(r["net_pnl"] for r in records if r["net_pnl"] < 0))

        return {
            "follower_address": follower_address,
            "filter_symbol": symbol,
            "summary": {
                "count": len(records),
                "wins": wins,
                "losses": len(records) - wins,
                "win_rate_pct": round(wins / len(records) * 100, 1) if records else 0,
                "total_net_pnl": round(total_net, 4),
                "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss > 0 else (None if not records else 9.99),
                "avg_roi_pct": round(
                    sum(r["roi_pct"] for r in records if r["roi_pct"]) / len(records), 2
                ) if records else 0,
            },
            "records": records,
        }
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.get("/{follower_address}/equity")
async def get_equity_chart(
    follower_address: str,
    days: int = Query(7, ge=1, le=90),
):
    """자산 추이 차트 데이터 (15분 간격 스냅샷)"""
    _validate_addr(follower_address)
    db = await _get_db()
    try:
        from db.pnl_tracker import get_equity_chart
        chart = await get_equity_chart(db, follower_address, days)

        if not chart:
            return {
                "follower_address": follower_address,
                "message": "No snapshot available — recorded automatically on trade fill",
                "chart": [],
            }

        start_eq = chart[0]["equity_usdc"]
        end_eq   = chart[-1]["equity_usdc"]
        roi = (end_eq - start_eq) / start_eq * 100 if start_eq > 0 else 0

        return {
            "follower_address": follower_address,
            "days": days,
            "start_equity": round(start_eq, 2),
            "end_equity": round(end_eq, 2),
            "total_roi_pct": round(roi, 2),
            "data_points": len(chart),
            "chart": chart,
        }
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.get("/{follower_address}/daily")
async def get_daily_stats(
    follower_address: str,
    days: int = Query(30, ge=1, le=365),
):
    """일별 집계 (Sharpe, MDD 포함)"""
    _validate_addr(follower_address)
    db = await _get_db()
    try:
        async with db.execute(
            """
            SELECT date_kst, starting_equity, ending_equity, daily_pnl,
                   daily_roi_pct, win_trades, lose_trades, total_trades,
                   win_rate, max_drawdown_pct, total_fee_usdc
            FROM daily_stats
            WHERE follower_address=?
            ORDER BY date_kst DESC LIMIT ?
            """,
            (follower_address, days)
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

        rows.reverse()  # 오름차순

        if not rows:
            return {"follower_address": follower_address, "message": "No daily aggregation available", "days": []}

        # Sharpe 계산
        rois = [r["daily_roi_pct"] for r in rows]
        import math
        mean_roi = sum(rois) / len(rois)
        std_roi  = math.sqrt(sum((r - mean_roi)**2 for r in rois) / len(rois)) if len(rois) > 1 else 1e-9
        sharpe = (mean_roi / std_roi) * math.sqrt(252) if std_roi > 1e-9 else 0

        # 누적 MDD
        peak = rows[0]["starting_equity"] or 1
        mdd = 0.0
        for r in rows:
            eq = r["ending_equity"] or peak
            if eq > peak: peak = eq
            dd = (peak - eq) / peak * 100
            if dd > mdd: mdd = dd

        return {
            "follower_address": follower_address,
            "period_days": len(rows),
            "sharpe_ratio": round(sharpe, 3),
            "max_drawdown_pct": round(mdd, 2),
            "total_realized_pnl": round(sum(r["daily_pnl"] for r in rows), 4),
            "days": rows,
        }
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.get("/{follower_address}/trades")
async def get_trade_history(
    follower_address: str,
    limit: int = Query(50, ge=1, le=500),
    status: str = Query("filled"),
    symbol: str = Query(None),
):
    """
    팔로워 거래 이력 (copy_trades 기반).
    실현 PnL은 /pnl 엔드포인트가 더 정확.
    """
    _validate_addr(follower_address)
    db = await _get_db()
    try:
        where = "WHERE follower_address=?"
        params: list = [follower_address]
        if status != "all":
            where += " AND status=?"
            params.append(status)
        if symbol:
            where += " AND symbol=?"
            params.append(symbol.upper())
        params.append(limit)

        async with db.execute(
            f"""SELECT id, symbol, side, amount, exec_price, entry_price,
                       realized_pnl, fee_usdc, position_action, hold_sec,
                       status, error_msg, created_at, filled_at
                FROM copy_trades {where}
                ORDER BY created_at DESC LIMIT ?""",
            params
        ) as cur:
            trades = [dict(r) for r in await cur.fetchall()]

        for t in trades:
            if t.get("created_at") and isinstance(t["created_at"], int) and t["created_at"] > 1e12:
                t["created_at_kst"] = datetime.fromtimestamp(
                    t["created_at"] / 1000
                ).strftime("%Y-%m-%d %H:%M:%S")

        filled = [t for t in trades if t["status"] == "filled"]
        wins  = [t for t in filled if (t.get("realized_pnl") or 0) > 0]
        total_pnl = sum(t.get("realized_pnl") or 0 for t in filled)

        return {
            "follower_address": follower_address,
            "filter": {"status": status, "symbol": symbol},
            "summary": {
                "returned": len(trades),
                "filled": len(filled),
                "failed": sum(1 for t in trades if t["status"] == "failed"),
                "total_realized_pnl": round(total_pnl, 4),
                "win_rate_pct": round(len(wins) / len(filled) * 100, 1) if filled else 0,
            },
            "trades": trades,
        }
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.post("/{follower_address}/snapshot")
async def trigger_snapshot(
    follower_address: str,
    body: SnapshotRequest = SnapshotRequest(),
):
    """수동 자산 스냅샷 트리거"""
    _validate_addr(follower_address)
    db = await _get_db()
    try:
        from db.pnl_tracker import take_equity_snapshot, get_performance_summary
        summary = await get_performance_summary(db, follower_address)
        equity = body.base_capital + summary.get("realized_pnl", 0) + summary.get("unrealized_pnl", 0)
        await take_equity_snapshot(db, follower_address, equity)
        return {
            "ok": True,
            "follower_address": follower_address,
            "equity_usdc": round(equity, 2),
            "realized_pnl": summary.get("realized_pnl", 0),
            "unrealized_pnl": summary.get("unrealized_pnl", 0),
            "snapshot_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception as e:
        logger.exception(f"snapshot 오류: {follower_address}")
        raise HTTPException(500, detail=str(e))
