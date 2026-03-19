"""
PnL 라우터 — 팔로워 실적 조회/스냅샷 API

엔드포인트:
  GET  /pnl/{follower_address}/summary    — 팔로워 PnL 요약 (최근 N일)
  GET  /pnl/{follower_address}/history    — 일별 PnL 이력
  GET  /pnl/{follower_address}/by-trader  — 트레이더별 PnL 집계
  GET  /pnl/{follower_address}/trades     — 복사 거래 내역 (페이지네이션)
  POST /pnl/{follower_address}/snapshot   — 오늘 PnL 스냅샷 강제 저장
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pnl", tags=["pnl"])


def _get_db():
    """api.main 에서 _db 가져오기. 없으면 503."""
    try:
        from api.main import _db
        if _db is None:
            raise HTTPException(status_code=503, detail={"error": "DB가 초기화되지 않았습니다", "code": "SERVICE_UNAVAILABLE"})
        return _db
    except ImportError:
        raise HTTPException(status_code=503, detail={"error": "DB 모듈 로드 실패", "code": "SERVICE_UNAVAILABLE"})


@router.get("/{follower_address}/summary")
async def get_pnl_summary(
    follower_address: str,
    days: int = Query(default=30, ge=1, le=365),
) -> dict:
    """팔로워 PnL 요약 (최근 N일)"""
    db = _get_db()
    try:
        import aiosqlite
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

        async with db.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN pnl IS NOT NULL THEN pnl ELSE 0 END), 0) as total_pnl,
                COUNT(*) as total_trades,
                SUM(CASE WHEN pnl IS NOT NULL AND pnl > 0 THEN 1 ELSE 0 END) as win_count,
                SUM(CASE WHEN pnl IS NOT NULL AND pnl <= 0 THEN 1 ELSE 0 END) as loss_count,
                COALESCE(SUM(
                    CASE WHEN amount IS NOT NULL AND price IS NOT NULL
                    THEN CAST(amount AS REAL) * CAST(price AS REAL)
                    ELSE 0 END
                ), 0) as volume_usdc
            FROM copy_trades
            WHERE follower_address = ?
              AND status = 'filled'
              AND date(created_at / 1000, 'unixepoch') >= ?
        """, (follower_address, cutoff)) as cur:
            row = await cur.fetchone()

        if row is None:
            return {"follower_address": follower_address, "days": days,
                    "total_pnl": 0.0, "win_rate": 0.0, "total_trades": 0,
                    "win_count": 0, "loss_count": 0, "volume_usdc": 0.0}

        r = dict(row)
        total = int(r.get("total_trades") or 0)
        wins  = int(r.get("win_count") or 0)
        win_rate = wins / total if total > 0 else 0.0
        return {
            "follower_address": follower_address,
            "days":        days,
            "total_pnl":   float(r.get("total_pnl") or 0),
            "win_rate":    round(win_rate, 4),
            "total_trades": total,
            "win_count":   wins,
            "loss_count":  int(r.get("loss_count") or 0),
            "volume_usdc": float(r.get("volume_usdc") or 0),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[PnL] summary 조회 오류: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "PnL 요약 조회 실패", "code": "INTERNAL_SERVER_ERROR"})


@router.get("/{follower_address}/history")
async def get_pnl_history(
    follower_address: str,
    days: int = Query(default=30, ge=1, le=365),
) -> dict:
    """일별 PnL 이력 반환"""
    db = _get_db()
    try:
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

        # follower_pnl_daily 우선
        async with db.execute("""
            SELECT date, realized_pnl, cumulative_pnl, trade_count, win_count
            FROM follower_pnl_daily
            WHERE follower_address = ? AND date >= ?
            ORDER BY date ASC
        """, (follower_address, cutoff)) as cur:
            rows = await cur.fetchall()

        if rows:
            data = [dict(r) for r in rows]
        else:
            # fallback: copy_trades 일별 집계
            async with db.execute("""
                SELECT
                    date(created_at / 1000, 'unixepoch') as date,
                    COALESCE(SUM(CASE WHEN pnl IS NOT NULL THEN pnl ELSE 0 END), 0) as realized_pnl,
                    COUNT(*) as trade_count,
                    SUM(CASE WHEN pnl IS NOT NULL AND pnl > 0 THEN 1 ELSE 0 END) as win_count
                FROM copy_trades
                WHERE follower_address = ?
                  AND status = 'filled'
                  AND date(created_at / 1000, 'unixepoch') >= ?
                GROUP BY date(created_at / 1000, 'unixepoch')
                ORDER BY date ASC
            """, (follower_address, cutoff)) as cur:
                rows = await cur.fetchall()

            cumulative = 0.0
            data = []
            for row in rows:
                r = dict(row)
                cumulative += float(r.get("realized_pnl") or 0)
                data.append({
                    "date":           r["date"],
                    "realized_pnl":   float(r.get("realized_pnl") or 0),
                    "cumulative_pnl": round(cumulative, 4),
                    "trade_count":    int(r.get("trade_count") or 0),
                    "win_count":      int(r.get("win_count") or 0),
                })

        return {"follower_address": follower_address, "days": days, "data": data, "count": len(data)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[PnL] history 조회 오류: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "PnL 이력 조회 실패", "code": "INTERNAL_SERVER_ERROR"})


@router.get("/{follower_address}/by-trader")
async def get_pnl_by_trader(follower_address: str) -> dict:
    """트레이더별 PnL 집계"""
    db = _get_db()
    try:
        async with db.execute("""
            SELECT
                trader_address,
                COALESCE(SUM(CASE WHEN pnl IS NOT NULL THEN pnl ELSE 0 END), 0) as total_pnl,
                COUNT(*) as trades,
                SUM(CASE WHEN pnl IS NOT NULL AND pnl > 0 THEN 1 ELSE 0 END) as win_count
            FROM copy_trades
            WHERE follower_address = ? AND status = 'filled'
            GROUP BY trader_address
        """, (follower_address,)) as cur:
            rows = await cur.fetchall()

        data = []
        for row in rows:
            r = dict(row)
            total = int(r.get("trades") or 0)
            wins  = int(r.get("win_count") or 0)
            win_rate = wins / total if total > 0 else 0.0
            data.append({
                "trader_address": r["trader_address"],
                "total_pnl":     float(r.get("total_pnl") or 0),
                "trades":        total,
                "win_rate":      round(win_rate, 4),
            })

        return {"follower_address": follower_address, "data": data, "count": len(data)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[PnL] by-trader 조회 오류: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "트레이더별 PnL 조회 실패", "code": "INTERNAL_SERVER_ERROR"})


@router.get("/{follower_address}/trades")
async def get_pnl_trades(
    follower_address: str,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    status: Optional[str] = Query(default=None),
) -> dict:
    """팔로워 복사 거래 내역 (페이지네이션)"""
    if status and status not in ("filled", "pending", "failed"):
        raise HTTPException(
            status_code=400,
            detail={"error": "status는 filled | pending | failed 중 하나여야 합니다", "code": "INVALID_STATUS"}
        )
    db = _get_db()
    try:
        conditions = ["follower_address = ?"]
        params: list = [follower_address]
        if status:
            conditions.append("status = ?")
            params.append(status)

        where = " AND ".join(conditions)
        params_with_limit = params + [limit, offset]

        async with db.execute(
            f"SELECT * FROM copy_trades WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params_with_limit
        ) as cur:
            rows = await cur.fetchall()

        # 전체 카운트
        async with db.execute(
            f"SELECT COUNT(*) as cnt FROM copy_trades WHERE {where}", params
        ) as cur2:
            total_row = await cur2.fetchone()
        total = int(dict(total_row)["cnt"]) if total_row else 0

        data = [dict(r) for r in rows]
        return {
            "follower_address": follower_address,
            "data": data,
            "count": len(data),
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[PnL] trades 조회 오류: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "거래 내역 조회 실패", "code": "INTERNAL_SERVER_ERROR"})


@router.post("/{follower_address}/snapshot")
async def snapshot_pnl(
    follower_address: str,
    date: Optional[str] = Query(default=None, description="YYYY-MM-DD (UTC), 기본값: 오늘"),
) -> dict:
    """오늘 PnL을 follower_pnl_daily에 스냅샷"""
    db = _get_db()
    try:
        from datetime import datetime, timezone
        target_date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

        async with db.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN pnl IS NOT NULL THEN pnl ELSE 0 END), 0) as realized_pnl,
                COUNT(*) as trade_count,
                SUM(CASE WHEN pnl IS NOT NULL AND pnl > 0 THEN 1 ELSE 0 END) as win_count,
                SUM(CASE WHEN pnl IS NOT NULL AND pnl <= 0 THEN 1 ELSE 0 END) as loss_count,
                COALESCE(SUM(
                    CASE WHEN amount IS NOT NULL AND price IS NOT NULL
                    THEN CAST(amount AS REAL) * CAST(price AS REAL)
                    ELSE 0 END
                ), 0) as volume_usdc
            FROM copy_trades
            WHERE follower_address = ?
              AND status = 'filled'
              AND date(created_at / 1000, 'unixepoch') = ?
        """, (follower_address, target_date)) as cur:
            row = await cur.fetchone()

        r = dict(row) if row else {}
        realized_pnl = float(r.get("realized_pnl") or 0)

        # 누적 PnL
        async with db.execute("""
            SELECT COALESCE(SUM(realized_pnl), 0) as cum
            FROM follower_pnl_daily
            WHERE follower_address = ? AND date < ?
        """, (follower_address, target_date)) as cur2:
            prev_row = await cur2.fetchone()
        prev_cum = float(dict(prev_row)["cum"] or 0) if prev_row else 0.0
        cumulative = prev_cum + realized_pnl

        await db.execute("""
            INSERT INTO follower_pnl_daily
                (follower_address, date, realized_pnl, cumulative_pnl,
                 trade_count, win_count, loss_count, volume_usdc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(follower_address, date) DO UPDATE SET
                realized_pnl   = excluded.realized_pnl,
                cumulative_pnl = excluded.cumulative_pnl,
                trade_count    = excluded.trade_count,
                win_count      = excluded.win_count,
                loss_count     = excluded.loss_count,
                volume_usdc    = excluded.volume_usdc,
                synced_at      = CURRENT_TIMESTAMP
        """, (
            follower_address, target_date,
            realized_pnl,
            round(cumulative, 4),
            int(r.get("trade_count") or 0),
            int(r.get("win_count") or 0),
            int(r.get("loss_count") or 0),
            float(r.get("volume_usdc") or 0),
        ))
        await db.commit()

        return {
            "ok": True,
            "follower_address": follower_address,
            "date": target_date,
            "realized_pnl": realized_pnl,
            "cumulative_pnl": round(cumulative, 4),
            "trade_count": int(r.get("trade_count") or 0),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[PnL] snapshot 오류: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail={"error": "PnL 스냅샷 저장 실패", "code": "INTERNAL_SERVER_ERROR"})
