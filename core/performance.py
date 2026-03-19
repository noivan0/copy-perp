"""
core/performance.py — 팔로워 실적 기록 & 분석 엔진

기능:
  1. record_follower_snapshot()   — 팔로워 일별 자본 스냅샷 (cron 또는 trade 완료 시)
  2. calc_follower_stats()        — 팔로워 종합 성과 지표 (ROI, Sharpe, MaxDD, Win Rate ...)
  3. get_performance_report()     — 인간 친화적 리포트 dict (API 응답용)
  4. rank_followers()             — 팔로워 랭킹 (플랫폼 신뢰도 강화용)
  5. get_best_trader_for_follower() — 팔로워 수익 기준 최적 트레이더 추천
"""

import math
import time
import logging
from typing import Optional

import aiosqlite

logger = logging.getLogger(__name__)

# ── SQL 마이그레이션 ──────────────────────────────────────────────────────────

_CREATE_FOLLOWER_SNAPSHOTS = (
    "CREATE TABLE IF NOT EXISTS follower_snapshots ("
    "  id               INTEGER PRIMARY KEY AUTOINCREMENT,"
    "  follower_address TEXT NOT NULL,"
    "  date             TEXT NOT NULL,"
    "  equity           REAL DEFAULT 0,"
    "  realized_pnl     REAL DEFAULT 0,"
    "  unrealized_pnl   REAL DEFAULT 0,"
    "  trade_count      INTEGER DEFAULT 0,"
    "  win_count        INTEGER DEFAULT 0,"
    "  loss_count       INTEGER DEFAULT 0,"
    "  fee_paid         REAL DEFAULT 0,"
    "  synced_at        INTEGER DEFAULT 0,"
    "  UNIQUE(follower_address, date)"
    ")"
)

_CREATE_FOLLOWER_PERFORMANCE = (
    "CREATE TABLE IF NOT EXISTS follower_performance ("
    "  follower_address TEXT PRIMARY KEY,"
    "  initial_capital  REAL DEFAULT 0,"
    "  current_equity   REAL DEFAULT 0,"
    "  total_pnl        REAL DEFAULT 0,"
    "  total_roi_pct    REAL DEFAULT 0,"
    "  win_count        INTEGER DEFAULT 0,"
    "  loss_count       INTEGER DEFAULT 0,"
    "  win_rate_pct     REAL DEFAULT 0,"
    "  best_day_pnl     REAL DEFAULT 0,"
    "  worst_day_pnl    REAL DEFAULT 0,"
    "  max_drawdown_pct REAL DEFAULT 0,"
    "  sharpe_ratio     REAL DEFAULT 0,"
    "  calmar_ratio     REAL DEFAULT 0,"
    "  profit_factor    REAL DEFAULT 0,"
    "  avg_win_usdc     REAL DEFAULT 0,"
    "  avg_loss_usdc    REAL DEFAULT 0,"
    "  streak_current   INTEGER DEFAULT 0,"
    "  streak_best      INTEGER DEFAULT 0,"
    "  total_fee_paid   REAL DEFAULT 0,"
    "  total_trades     INTEGER DEFAULT 0,"
    "  active_days      INTEGER DEFAULT 0,"
    "  first_trade_at   INTEGER DEFAULT 0,"
    "  last_trade_at    INTEGER DEFAULT 0,"
    "  updated_at       INTEGER DEFAULT 0"
    ")"
)

PERF_MIGRATIONS = [
    _CREATE_FOLLOWER_SNAPSHOTS,
    _CREATE_FOLLOWER_PERFORMANCE,
    "ALTER TABLE copy_trades ADD COLUMN hold_seconds INTEGER DEFAULT 0",
    "ALTER TABLE copy_trades ADD COLUMN fee_usdc REAL DEFAULT 0",
]


async def apply_perf_migrations(conn: aiosqlite.Connection) -> None:
    """DB 마이그레이션 — 이미 존재하면 무시"""
    for sql in PERF_MIGRATIONS:
        try:
            await conn.execute(sql.strip())
        except Exception:
            pass
    await conn.commit()


# ── 핵심 함수 ─────────────────────────────────────────────────────────────────

async def record_follower_snapshot(
    conn: aiosqlite.Connection,
    follower_address: str,
    base_capital: float = 0.0,
) -> dict:
    """
    오늘(UTC) 팔로워 copy_trades에서 실현 PnL 집계 → follower_snapshots 저장
    → follower_performance 캐시 갱신

    Returns: 오늘 스냅샷 dict
    """
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now_ts = int(time.time() * 1000)

    # ① 오늘 체결된 거래 집계
    async with conn.execute(
        """
        SELECT
            COUNT(*)                                        AS trade_count,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)      AS win_count,
            SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END)      AS loss_count,
            COALESCE(SUM(pnl), 0)                          AS realized_pnl,
            COALESCE(SUM(fee_usdc), 0)                     AS fee_paid
        FROM copy_trades
        WHERE follower_address = ?
          AND status = 'filled'
          AND date(created_at / 1000, 'unixepoch') = ?
        """,
        (follower_address, today),
    ) as cur:
        row = dict(await cur.fetchone())

    # ② 누적 PnL로 현재 추정 equity 계산
    async with conn.execute(
        "SELECT COALESCE(SUM(pnl), 0) FROM copy_trades WHERE follower_address=? AND status='filled'",
        (follower_address,)
    ) as cur:
        total_pnl = (await cur.fetchone())[0] or 0.0

    equity = (base_capital or 10000.0) + float(total_pnl)

    snap = {
        "follower_address": follower_address,
        "date":             today,
        "equity":           round(equity, 4),
        "realized_pnl":     round(float(row["realized_pnl"]), 4),
        "unrealized_pnl":   0.0,
        "trade_count":      int(row["trade_count"] or 0),
        "win_count":        int(row["win_count"] or 0),
        "loss_count":       int(row["loss_count"] or 0),
        "fee_paid":         round(float(row["fee_paid"]), 4),
        "synced_at":        now_ts,
    }

    await conn.execute(
        """
        INSERT OR REPLACE INTO follower_snapshots
        (follower_address, date, equity, realized_pnl, unrealized_pnl,
         trade_count, win_count, loss_count, fee_paid, synced_at)
        VALUES (:follower_address, :date, :equity, :realized_pnl, :unrealized_pnl,
                :trade_count, :win_count, :loss_count, :fee_paid, :synced_at)
        """,
        snap,
    )
    await conn.commit()

    # ③ follower_performance 캐시 갱신
    await _refresh_performance_cache(conn, follower_address, base_capital)

    return snap


async def _refresh_performance_cache(
    conn: aiosqlite.Connection,
    follower_address: str,
    base_capital: float = 0.0,
) -> None:
    """follower_performance 테이블을 스냅샷 이력에서 재계산"""
    now_ts = int(time.time() * 1000)

    # 전체 스냅샷 이력
    async with conn.execute(
        """
        SELECT date, equity, realized_pnl, trade_count, win_count, loss_count, fee_paid
        FROM follower_snapshots
        WHERE follower_address = ?
        ORDER BY date ASC
        """,
        (follower_address,),
    ) as cur:
        snaps = [dict(r) for r in await cur.fetchall()]

    if not snaps:
        return

    # 전체 copy_trades 집계
    async with conn.execute(
        """
        SELECT
            COUNT(*)                                    AS total_trades,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)   AS win_count,
            SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END)   AS loss_count,
            COALESCE(SUM(pnl), 0)                       AS total_pnl,
            COALESCE(SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END), 0) AS gross_profit,
            COALESCE(SUM(CASE WHEN pnl < 0 THEN ABS(pnl) ELSE 0 END), 0) AS gross_loss,
            COALESCE(SUM(fee_usdc), 0)                  AS total_fee,
            MIN(created_at)                             AS first_at,
            MAX(created_at)                             AS last_at
        FROM copy_trades
        WHERE follower_address = ? AND status = 'filled'
        """,
        (follower_address,),
    ) as cur:
        t = dict(await cur.fetchone())

    total_trades  = int(t["total_trades"] or 0)
    win_count     = int(t["win_count"] or 0)
    loss_count    = int(t["loss_count"] or 0)
    total_pnl     = float(t["total_pnl"] or 0)
    gross_profit  = float(t["gross_profit"] or 0)
    gross_loss    = float(t["gross_loss"] or 0)
    total_fee     = float(t["total_fee"] or 0)

    win_rate_pct  = round(win_count / total_trades * 100, 2) if total_trades > 0 else 0.0
    profit_factor = round(gross_profit / gross_loss, 3) if gross_loss > 0 else (9.99 if gross_profit > 0 else 0.0)
    avg_win       = round(gross_profit / win_count, 4) if win_count > 0 else 0.0
    avg_loss      = round(gross_loss / loss_count, 4) if loss_count > 0 else 0.0

    # 자본 곡선 기반 지표
    initial_capital = base_capital or 10000.0
    current_equity  = initial_capital + total_pnl
    total_roi_pct   = round(total_pnl / initial_capital * 100, 4) if initial_capital > 0 else 0.0

    # 일별 PnL 리스트 (Sharpe / MaxDD 계산)
    daily_pnls    = [float(s["realized_pnl"]) for s in snaps]
    best_day_pnl  = max(daily_pnls) if daily_pnls else 0.0
    worst_day_pnl = min(daily_pnls) if daily_pnls else 0.0

    # 최대 낙폭 (equity 기준)
    equities = [initial_capital] + [float(s["equity"]) for s in snaps]
    max_dd_pct = 0.0
    peak = equities[0]
    for eq in equities:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak * 100
            if dd > max_dd_pct:
                max_dd_pct = dd

    # Sharpe (연환산, 일별 수익률 기준)
    sharpe_ratio = 0.0
    if len(daily_pnls) >= 5:
        cap = initial_capital
        daily_rets = []
        for eq, pnl in zip([float(s["equity"]) for s in snaps], daily_pnls):
            if cap > 0:
                daily_rets.append(pnl / cap)
            cap = eq
        if daily_rets:
            n = len(daily_rets)
            mean = sum(daily_rets) / n
            variance = sum((r - mean) ** 2 for r in daily_rets) / n
            std = math.sqrt(variance) if variance > 0 else 0
            sharpe_ratio = round(mean / std * math.sqrt(252), 3) if std > 0 else 0.0

    # Calmar Ratio
    calmar_ratio = round(total_roi_pct / max_dd_pct, 3) if max_dd_pct > 0 else 0.0

    # 연속 수익/손실 스트릭
    async with conn.execute(
        "SELECT pnl FROM copy_trades WHERE follower_address=? AND status='filled' ORDER BY created_at DESC LIMIT 100",
        (follower_address,),
    ) as cur:
        recent_pnls = [float(r[0] or 0) for r in await cur.fetchall()]

    streak_current = 0
    streak_best    = 0
    if recent_pnls:
        sign = 1 if recent_pnls[0] >= 0 else -1
        for p in recent_pnls:
            s = 1 if p >= 0 else -1
            if s == sign:
                streak_current += 1
            else:
                break
        streak_current *= sign  # 양수=연속수익, 음수=연속손실

        # 최대 연속 수익 스트릭
        cur_streak = 0
        for p in reversed(recent_pnls):
            if p >= 0:
                cur_streak += 1
                streak_best = max(streak_best, cur_streak)
            else:
                cur_streak = 0

    perf = {
        "follower_address": follower_address,
        "initial_capital":  round(initial_capital, 2),
        "current_equity":   round(current_equity, 2),
        "total_pnl":        round(total_pnl, 4),
        "total_roi_pct":    total_roi_pct,
        "win_count":        win_count,
        "loss_count":       loss_count,
        "win_rate_pct":     win_rate_pct,
        "best_day_pnl":     round(best_day_pnl, 4),
        "worst_day_pnl":    round(worst_day_pnl, 4),
        "max_drawdown_pct": round(max_dd_pct, 4),
        "sharpe_ratio":     sharpe_ratio,
        "calmar_ratio":     calmar_ratio,
        "profit_factor":    profit_factor,
        "avg_win_usdc":     avg_win,
        "avg_loss_usdc":    avg_loss,
        "streak_current":   streak_current,
        "streak_best":      streak_best,
        "total_fee_paid":   round(total_fee, 4),
        "total_trades":     total_trades,
        "active_days":      len(snaps),
        "first_trade_at":   int(t["first_at"] or 0),
        "last_trade_at":    int(t["last_at"] or 0),
        "updated_at":       now_ts,
    }

    await conn.execute(
        """
        INSERT OR REPLACE INTO follower_performance
        (follower_address, initial_capital, current_equity, total_pnl, total_roi_pct,
         win_count, loss_count, win_rate_pct, best_day_pnl, worst_day_pnl,
         max_drawdown_pct, sharpe_ratio, calmar_ratio, profit_factor,
         avg_win_usdc, avg_loss_usdc, streak_current, streak_best,
         total_fee_paid, total_trades, active_days,
         first_trade_at, last_trade_at, updated_at)
        VALUES
        (:follower_address, :initial_capital, :current_equity, :total_pnl, :total_roi_pct,
         :win_count, :loss_count, :win_rate_pct, :best_day_pnl, :worst_day_pnl,
         :max_drawdown_pct, :sharpe_ratio, :calmar_ratio, :profit_factor,
         :avg_win_usdc, :avg_loss_usdc, :streak_current, :streak_best,
         :total_fee_paid, :total_trades, :active_days,
         :first_trade_at, :last_trade_at, :updated_at)
        """,
        perf,
    )
    await conn.commit()


async def get_performance_report(
    conn: aiosqlite.Connection,
    follower_address: str,
    base_capital: float = 0.0,
    days: int = 30,
) -> dict:
    """
    API 응답용 성과 리포트

    Returns: {
      summary: {...},          # 핵심 지표
      equity_curve: [...],     # 일별 자본 곡선
      daily_pnl: [...],        # 일별 PnL
      recent_trades: [...],    # 최근 10건 거래
      badge: str,              # 성과 뱃지 (🏆 Elite / ⭐ Top / ✅ Good / 🌱 Growing)
      message: str,            # 인간 친화적 요약 메시지
    }
    """
    # 스냅샷이 없으면 먼저 생성
    async with conn.execute(
        "SELECT COUNT(*) FROM follower_snapshots WHERE follower_address=?",
        (follower_address,)
    ) as cur:
        count = (await cur.fetchone())[0]

    if count == 0:
        await record_follower_snapshot(conn, follower_address, base_capital)

    # 성과 캐시 읽기
    async with conn.execute(
        "SELECT * FROM follower_performance WHERE follower_address=?",
        (follower_address,)
    ) as cur:
        row = await cur.fetchone()

    perf = dict(row) if row else {}

    # 일별 자본 곡선 (최근 N일)
    async with conn.execute(
        """
        SELECT date, equity, realized_pnl, trade_count, win_count, loss_count, fee_paid
        FROM follower_snapshots
        WHERE follower_address = ?
        ORDER BY date DESC
        LIMIT ?
        """,
        (follower_address, days),
    ) as cur:
        snaps = [dict(r) for r in await cur.fetchall()]
    snaps.reverse()  # 오래된 순서로

    equity_curve = [{"date": s["date"], "equity": round(float(s["equity"]), 2)} for s in snaps]
    daily_pnl    = [{"date": s["date"], "pnl": round(float(s["realized_pnl"]), 4),
                     "trades": int(s["trade_count"] or 0)} for s in snaps]

    # 최근 거래 10건
    async with conn.execute(
        """
        SELECT id, symbol, side, amount, pnl, status, exec_price, entry_price,
               created_at, hold_seconds, fee_usdc
        FROM copy_trades
        WHERE follower_address = ?
        ORDER BY created_at DESC
        LIMIT 10
        """,
        (follower_address,),
    ) as cur:
        recent_trades = [dict(r) for r in await cur.fetchall()]

    # 뱃지 결정
    roi = float(perf.get("total_roi_pct", 0))
    sharpe = float(perf.get("sharpe_ratio", 0))
    wr = float(perf.get("win_rate_pct", 0))

    if roi >= 10 and sharpe >= 1.5 and wr >= 60:
        badge = "🏆 Elite Copier"
    elif roi >= 5 and sharpe >= 1.0:
        badge = "⭐ Top Performer"
    elif roi >= 0:
        badge = "✅ Profitable"
    else:
        badge = "🌱 Growing"

    # 인간 친화적 메시지
    total_pnl = float(perf.get("total_pnl", 0))
    sign = "+" if total_pnl >= 0 else ""
    trades = int(perf.get("total_trades", 0))
    active_days = int(perf.get("active_days", 0))

    if total_pnl > 0:
        message = (
            f"{active_days}일 동안 {trades}건 거래, "
            f"총 {sign}${total_pnl:,.2f} USDC 수익 달성 🎉"
        )
    elif total_pnl == 0:
        message = "아직 체결된 거래가 없습니다. 트레이더를 팔로우하고 시작하세요!"
    else:
        message = (
            f"{active_days}일 동안 {trades}건 거래, "
            f"현재 ${abs(total_pnl):,.2f} USDC 손실 중. 전략 재검토를 권장합니다."
        )

    return {
        "follower_address": follower_address,
        "badge":   badge,
        "message": message,
        "summary": {
            "initial_capital":   perf.get("initial_capital", base_capital),
            "current_equity":    perf.get("current_equity", base_capital),
            "total_pnl":         perf.get("total_pnl", 0),
            "total_roi_pct":     perf.get("total_roi_pct", 0),
            "win_rate_pct":      perf.get("win_rate_pct", 0),
            "profit_factor":     perf.get("profit_factor", 0),
            "sharpe_ratio":      perf.get("sharpe_ratio", 0),
            "calmar_ratio":      perf.get("calmar_ratio", 0),
            "max_drawdown_pct":  perf.get("max_drawdown_pct", 0),
            "total_trades":      perf.get("total_trades", 0),
            "win_count":         perf.get("win_count", 0),
            "loss_count":        perf.get("loss_count", 0),
            "avg_win_usdc":      perf.get("avg_win_usdc", 0),
            "avg_loss_usdc":     perf.get("avg_loss_usdc", 0),
            "best_day_pnl":      perf.get("best_day_pnl", 0),
            "worst_day_pnl":     perf.get("worst_day_pnl", 0),
            "streak_current":    perf.get("streak_current", 0),
            "streak_best":       perf.get("streak_best", 0),
            "total_fee_paid":    perf.get("total_fee_paid", 0),
            "active_days":       perf.get("active_days", 0),
        },
        "equity_curve":   equity_curve,
        "daily_pnl":      daily_pnl,
        "recent_trades":  recent_trades,
        "generated_at":   int(time.time() * 1000),
    }


async def rank_followers(
    conn: aiosqlite.Connection,
    limit: int = 20,
) -> list:
    """
    팔로워 랭킹 (플랫폼 신뢰도 강화용 — "사람들이 이렇게 벌고 있다" 증명)

    Returns: [{rank, follower_address, total_roi_pct, total_pnl, sharpe, badge, ...}]
    """
    async with conn.execute(
        """
        SELECT follower_address, total_roi_pct, total_pnl, sharpe_ratio,
               win_rate_pct, profit_factor, max_drawdown_pct, total_trades, active_days
        FROM follower_performance
        WHERE total_trades > 0
        ORDER BY total_roi_pct DESC, sharpe_ratio DESC
        LIMIT ?
        """,
        (limit,),
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]

    result = []
    for i, r in enumerate(rows):
        roi    = float(r.get("total_roi_pct", 0))
        sharpe = float(r.get("sharpe_ratio", 0))
        wr     = float(r.get("win_rate_pct", 0))

        if roi >= 10 and sharpe >= 1.5 and wr >= 60:
            badge = "🏆 Elite"
        elif roi >= 5 and sharpe >= 1.0:
            badge = "⭐ Top"
        elif roi >= 0:
            badge = "✅ Good"
        else:
            badge = "🌱 Growing"

        # 프라이버시: 주소 마스킹
        addr = r["follower_address"]
        masked = addr[:6] + "..." + addr[-4:] if len(addr) > 10 else addr

        result.append({
            "rank":            i + 1,
            "follower_masked": masked,
            "badge":           badge,
            "total_roi_pct":   round(roi, 2),
            "total_pnl":       round(float(r.get("total_pnl", 0)), 2),
            "sharpe_ratio":    round(sharpe, 2),
            "win_rate_pct":    round(wr, 2),
            "profit_factor":   round(float(r.get("profit_factor", 0)), 2),
            "max_drawdown_pct": round(float(r.get("max_drawdown_pct", 0)), 2),
            "total_trades":    int(r.get("total_trades", 0)),
            "active_days":     int(r.get("active_days", 0)),
        })

    return result


async def get_platform_stats_enhanced(conn: aiosqlite.Connection) -> dict:
    """
    플랫폼 전체 통계 (신뢰도 강화용 대시보드 데이터)
    """
    async with conn.execute(
        """
        SELECT
            COUNT(*)                                AS total_followers,
            SUM(CASE WHEN total_roi_pct > 0 THEN 1 ELSE 0 END) AS profitable_followers,
            ROUND(AVG(total_roi_pct), 2)            AS avg_roi_pct,
            ROUND(AVG(win_rate_pct), 2)             AS avg_win_rate,
            ROUND(SUM(total_pnl), 2)                AS platform_total_pnl,
            ROUND(AVG(sharpe_ratio), 2)             AS avg_sharpe,
            MAX(total_roi_pct)                      AS best_roi_pct,
            SUM(total_trades)                       AS platform_total_trades
        FROM follower_performance
        WHERE total_trades > 0
        """
    ) as cur:
        row = dict(await cur.fetchone())

    total = int(row.get("total_followers") or 0)
    profitable = int(row.get("profitable_followers") or 0)
    profitability_rate = round(profitable / total * 100, 1) if total > 0 else 0.0

    return {
        "total_followers":       total,
        "profitable_followers":  profitable,
        "profitability_rate_pct": profitability_rate,
        "avg_roi_pct":           float(row.get("avg_roi_pct") or 0),
        "avg_win_rate_pct":      float(row.get("avg_win_rate") or 0),
        "avg_sharpe":            float(row.get("avg_sharpe") or 0),
        "platform_total_pnl":    float(row.get("platform_total_pnl") or 0),
        "best_roi_pct":          float(row.get("best_roi_pct") or 0),
        "platform_total_trades": int(row.get("platform_total_trades") or 0),
    }
