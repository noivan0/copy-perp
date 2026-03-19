"""
PnL Tracker — 실현/미실현 PnL 정확 기록 엔진

기능:
1. positions 테이블: 팔로워별 심볼별 열린 포지션 (평균 진입가, 수량)
2. pnl_records 테이블: 실현 PnL 상세 기록 (open/close 페어링)
3. equity_snapshots 테이블: 시간대별 자산 스냅샷 (차트용)
4. daily_stats 테이블: 일별 집계 (Sharpe, MDD 계산용)

PnL 계산 방식:
- LONG(bid):  realized_pnl = (close_price - entry_price) × size
- SHORT(ask): realized_pnl = (entry_price - close_price) × size
- 수수료: builder_fee + 0.05% 거래 수수료
"""

import aiosqlite
import time
import logging
from typing import Optional
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

MIGRATIONS = [
    # ── 포지션 테이블 ────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS positions (
        id                  TEXT PRIMARY KEY,
        follower_address    TEXT NOT NULL,
        trader_address      TEXT NOT NULL,
        symbol              TEXT NOT NULL,
        side                TEXT NOT NULL,          -- 'bid'(LONG) / 'ask'(SHORT)
        size                REAL NOT NULL DEFAULT 0, -- 현재 수량 (절댓값)
        avg_entry_price     REAL NOT NULL DEFAULT 0, -- 가중평균 진입가
        initial_size        REAL NOT NULL DEFAULT 0, -- 최초 진입 수량
        initial_entry_price REAL NOT NULL DEFAULT 0, -- 최초 진입가
        open_trade_id       TEXT,                   -- 오픈한 copy_trade id
        unrealized_pnl      REAL DEFAULT 0,
        mark_price          REAL DEFAULT 0,         -- 마지막 갱신 마크가격
        opened_at           INTEGER NOT NULL,       -- epoch ms
        last_updated        INTEGER NOT NULL,
        status              TEXT DEFAULT 'open',    -- 'open' / 'closed'
        UNIQUE(follower_address, symbol, status)
    )
    """,
    # ── 실현 PnL 기록 ────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS pnl_records (
        id                  TEXT PRIMARY KEY,
        follower_address    TEXT NOT NULL,
        trader_address      TEXT NOT NULL,
        symbol              TEXT NOT NULL,
        direction           TEXT NOT NULL,          -- 'long' / 'short'
        open_trade_id       TEXT,
        close_trade_id      TEXT,
        size                REAL NOT NULL,          -- 체결 수량
        entry_price         REAL NOT NULL,
        exit_price          REAL NOT NULL,
        gross_pnl           REAL NOT NULL,          -- 수수료 전 PnL
        fee_usdc            REAL DEFAULT 0,         -- 지불한 수수료
        builder_fee_usdc    REAL DEFAULT 0,         -- builder fee 분
        net_pnl             REAL NOT NULL,          -- 최종 PnL
        roi_pct             REAL,                   -- (net_pnl / cost_basis) × 100
        hold_duration_sec   INTEGER,                -- 보유 시간 (초)
        opened_at           INTEGER NOT NULL,
        closed_at           INTEGER NOT NULL,
        created_at          INTEGER NOT NULL
    )
    """,
    # ── 자산 스냅샷 (15분 간격) ──────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS equity_snapshots (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        follower_address    TEXT NOT NULL,
        equity_usdc         REAL NOT NULL,          -- 총 자산 (현금 + 미실현)
        realized_pnl_cum    REAL NOT NULL DEFAULT 0,-- 누적 실현 PnL
        unrealized_pnl      REAL DEFAULT 0,
        open_positions      INTEGER DEFAULT 0,
        snapshot_at         INTEGER NOT NULL,       -- epoch ms
        UNIQUE(follower_address, snapshot_at)
    )
    """,
    # ── 일별 집계 ────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS daily_stats (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        follower_address    TEXT NOT NULL,
        date_kst            TEXT NOT NULL,          -- 'YYYY-MM-DD'
        starting_equity     REAL DEFAULT 0,
        ending_equity       REAL DEFAULT 0,
        daily_pnl           REAL DEFAULT 0,
        daily_roi_pct       REAL DEFAULT 0,
        win_trades          INTEGER DEFAULT 0,
        lose_trades         INTEGER DEFAULT 0,
        total_trades        INTEGER DEFAULT 0,
        win_rate            REAL DEFAULT 0,
        max_drawdown_pct    REAL DEFAULT 0,
        total_fee_usdc      REAL DEFAULT 0,
        UNIQUE(follower_address, date_kst)
    )
    """,
    # ── copy_trades 에 누락 컬럼 추가 ────────────────────────
    "ALTER TABLE copy_trades ADD COLUMN close_price REAL",
    "ALTER TABLE copy_trades ADD COLUMN realized_pnl REAL",
    "ALTER TABLE copy_trades ADD COLUMN fee_usdc REAL",
    "ALTER TABLE copy_trades ADD COLUMN position_action TEXT",  -- 'open'/'reduce'/'close'/'flip'
    "ALTER TABLE copy_trades ADD COLUMN hold_sec INTEGER",
    "ALTER TABLE copy_trades ADD COLUMN filled_at_dt TEXT",     -- ISO datetime (가독성)
    "ALTER TABLE copy_trades ADD COLUMN created_at_dt TEXT",
    # ── 인덱스 ───────────────────────────────────────────────
    "CREATE INDEX IF NOT EXISTS idx_positions_follower_symbol ON positions(follower_address, symbol, status)",
    "CREATE INDEX IF NOT EXISTS idx_pnl_records_follower ON pnl_records(follower_address, closed_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_equity_snapshots_follower ON equity_snapshots(follower_address, snapshot_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_daily_stats_follower ON daily_stats(follower_address, date_kst DESC)",
]


async def apply_migrations(conn: aiosqlite.Connection):
    """PnL Tracker 마이그레이션 적용"""
    for sql in MIGRATIONS:
        try:
            await conn.execute(sql.strip())
        except Exception as e:
            if "duplicate column" not in str(e).lower() and "already exists" not in str(e).lower():
                logger.debug(f"마이그레이션 스킵 ({e}): {sql[:60]}...")
    await conn.commit()
    logger.info("PnL Tracker 마이그레이션 완료")


# ── 포지션 오픈/업데이트 ────────────────────────────────────
async def upsert_position(
    conn: aiosqlite.Connection,
    follower: str,
    trader: str,
    symbol: str,
    side: str,          # 'bid' / 'ask'
    size: float,        # 추가 수량
    exec_price: float,
    trade_id: str,
) -> dict:
    """
    포지션 오픈 또는 추가 매수 (평균 진입가 갱신)
    Returns: 현재 포지션 dict
    """
    now_ms = int(time.time() * 1000)
    pos_id = f"{follower[:8]}-{symbol}-open"

    async with conn.execute(
        "SELECT * FROM positions WHERE follower_address=? AND symbol=? AND status='open'",
        (follower, symbol)
    ) as cur:
        existing = await cur.fetchone()

    if existing:
        existing = dict(existing)
        old_size  = float(existing["size"])
        old_price = float(existing["avg_entry_price"])

        # 같은 방향이면 평균 진입가 갱신 (가중평균)
        if existing["side"] == side:
            new_size  = old_size + size
            new_price = (old_price * old_size + exec_price * size) / new_size
            await conn.execute(
                """UPDATE positions SET size=?, avg_entry_price=?, mark_price=?, last_updated=?
                   WHERE follower_address=? AND symbol=? AND status='open'""",
                (new_size, new_price, exec_price, now_ms, follower, symbol)
            )
            await conn.commit()
            return {"action": "add", "size": new_size, "avg_price": new_price}

        else:
            # 반대 방향 → 부분 청산 or 전환 (flip)
            return await _handle_direction_change(
                conn, existing, follower, trader, symbol, side, size, exec_price, trade_id, now_ms
            )
    else:
        # 신규 포지션
        import uuid
        new_id = str(uuid.uuid4())
        await conn.execute(
            """INSERT INTO positions
               (id, follower_address, trader_address, symbol, side, size,
                avg_entry_price, initial_size, initial_entry_price,
                open_trade_id, mark_price, opened_at, last_updated, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (new_id, follower, trader, symbol, side, size,
             exec_price, size, exec_price,
             trade_id, exec_price, now_ms, now_ms, "open")
        )
        await conn.commit()
        logger.info(f"[PnL] 포지션 OPEN: {follower[:8]} {symbol} {side} ×{size} @ {exec_price:.4f}")
        return {"action": "open", "size": size, "avg_price": exec_price}


async def _handle_direction_change(
    conn, existing, follower, trader, symbol, new_side, new_size, exec_price, trade_id, now_ms
):
    """반대 방향 주문: 청산 후 나머지는 신규 포지션으로"""
    old_size  = float(existing["size"])
    old_price = float(existing["avg_entry_price"])
    old_side  = existing["side"]
    old_opened_at = existing["opened_at"]

    close_size = min(old_size, new_size)
    pnl = _calc_pnl(old_side, old_price, exec_price, close_size)
    fee = close_size * exec_price * 0.0005  # 0.05% 거래 수수료

    await _record_pnl(conn, follower, trader, symbol, old_side, old_price, exec_price,
                      close_size, pnl, fee, 0.0, existing.get("open_trade_id"), trade_id,
                      old_opened_at, now_ms)

    remaining = old_size - close_size
    if remaining > 1e-8:
        await conn.execute(
            """UPDATE positions SET size=?, last_updated=? WHERE follower_address=? AND symbol=? AND status='open'""",
            (remaining, now_ms, follower, symbol)
        )
        action = "partial_close"
    else:
        await conn.execute(
            """UPDATE positions SET status='closed', last_updated=? WHERE follower_address=? AND symbol=? AND status='open'""",
            (now_ms, follower, symbol)
        )
        action = "close"

        # 나머지 반대 방향 신규 포지션
        if new_size > close_size:
            flip_size = new_size - close_size
            import uuid
            await conn.execute(
                """INSERT INTO positions
                   (id, follower_address, trader_address, symbol, side, size,
                    avg_entry_price, initial_size, initial_entry_price,
                    open_trade_id, mark_price, opened_at, last_updated, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), follower, trader, symbol, new_side, flip_size,
                 exec_price, flip_size, exec_price,
                 trade_id, exec_price, now_ms, now_ms, "open")
            )

    await conn.commit()
    return {"action": action, "realized_pnl": pnl, "fee": fee}


# ── 포지션 청산 (close_position) ─────────────────────────────
async def close_position(
    conn: aiosqlite.Connection,
    follower: str,
    trader: str,
    symbol: str,
    exec_price: float,
    close_size: Optional[float],  # None이면 전량
    trade_id: str,
    builder_fee: float = 0.0,
) -> Optional[dict]:
    """
    청산 처리 + pnl_records 기록 + copy_trades 업데이트
    Returns: {realized_pnl, net_pnl, roi_pct, hold_sec} or None
    """
    now_ms = int(time.time() * 1000)

    async with conn.execute(
        "SELECT * FROM positions WHERE follower_address=? AND symbol=? AND status='open'",
        (follower, symbol)
    ) as cur:
        pos = await cur.fetchone()

    if not pos:
        logger.debug(f"[PnL] 청산 대상 포지션 없음: {follower[:8]} {symbol}")
        return None

    pos = dict(pos)
    pos_side   = pos["side"]
    pos_size   = float(pos["size"])
    entry_price = float(pos["avg_entry_price"])
    opened_at  = pos["opened_at"]

    actual_close = close_size if (close_size and close_size < pos_size) else pos_size
    gross_pnl = _calc_pnl(pos_side, entry_price, exec_price, actual_close)
    fee = actual_close * exec_price * 0.0005  # 0.05% 거래 수수료
    net_pnl = gross_pnl - fee - builder_fee
    cost_basis = entry_price * actual_close
    roi_pct = (net_pnl / cost_basis * 100) if cost_basis > 0 else 0.0
    hold_sec = int((now_ms - opened_at) / 1000)

    await _record_pnl(
        conn, follower, trader, symbol, pos_side,
        entry_price, exec_price, actual_close,
        gross_pnl, fee, builder_fee,
        pos.get("open_trade_id"), trade_id,
        opened_at, now_ms
    )

    # 포지션 업데이트
    remaining = pos_size - actual_close
    if remaining > 1e-8:
        new_avg = entry_price  # 부분 청산 시 진입가 유지
        await conn.execute(
            "UPDATE positions SET size=?, last_updated=? WHERE follower_address=? AND symbol=? AND status='open'",
            (remaining, now_ms, follower, symbol)
        )
    else:
        await conn.execute(
            "UPDATE positions SET status='closed', last_updated=? WHERE follower_address=? AND symbol=? AND status='open'",
            (now_ms, follower, symbol)
        )

    # copy_trades 에 청산 정보 기록
    await conn.execute(
        """UPDATE copy_trades SET
           close_price=?, realized_pnl=?, fee_usdc=?,
           position_action='close', hold_sec=?,
           filled_at=?, filled_at_dt=?
           WHERE id=?""",
        (exec_price, net_pnl, fee + builder_fee, hold_sec,
         now_ms, datetime.fromtimestamp(now_ms/1000).isoformat(), trade_id)
    )
    await conn.commit()

    logger.info(
        f"[PnL] 청산: {follower[:8]} {symbol} {pos_side} "
        f"entry={entry_price:.4f} exit={exec_price:.4f} "
        f"gross={gross_pnl:+.4f} net={net_pnl:+.4f} ({roi_pct:+.2f}%) "
        f"hold={hold_sec//60}분"
    )

    return {"realized_pnl": gross_pnl, "net_pnl": net_pnl, "roi_pct": roi_pct, "hold_sec": hold_sec}


# ── 미실현 PnL 갱신 ─────────────────────────────────────────
async def update_unrealized(
    conn: aiosqlite.Connection,
    follower: str,
    symbol: str,
    mark_price: float,
):
    """마크가격 갱신 → 미실현 PnL 재계산"""
    async with conn.execute(
        "SELECT * FROM positions WHERE follower_address=? AND symbol=? AND status='open'",
        (follower, symbol)
    ) as cur:
        pos = await cur.fetchone()
    if not pos:
        return
    pos = dict(pos)
    unrealized = _calc_pnl(pos["side"], float(pos["avg_entry_price"]), mark_price, float(pos["size"]))
    now_ms = int(time.time() * 1000)
    await conn.execute(
        "UPDATE positions SET unrealized_pnl=?, mark_price=?, last_updated=? WHERE follower_address=? AND symbol=? AND status='open'",
        (unrealized, mark_price, now_ms, follower, symbol)
    )
    await conn.commit()


# ── 자산 스냅샷 ──────────────────────────────────────────────
async def take_equity_snapshot(
    conn: aiosqlite.Connection,
    follower: str,
    equity_usdc: float,
):
    """15분마다 자산 스냅샷 저장"""
    now_ms = int(time.time() * 1000)

    # 누적 실현 PnL
    async with conn.execute(
        "SELECT COALESCE(SUM(net_pnl),0) FROM pnl_records WHERE follower_address=?",
        (follower,)
    ) as cur:
        row = await cur.fetchone()
    cum_realized = float(row[0]) if row else 0.0

    # 미실현 PnL 합계
    async with conn.execute(
        "SELECT COALESCE(SUM(unrealized_pnl),0) FROM positions WHERE follower_address=? AND status='open'",
        (follower,)
    ) as cur:
        row = await cur.fetchone()
    unrealized = float(row[0]) if row else 0.0

    # 열린 포지션 수
    async with conn.execute(
        "SELECT COUNT(*) FROM positions WHERE follower_address=? AND status='open'",
        (follower,)
    ) as cur:
        row = await cur.fetchone()
    open_pos = int(row[0]) if row else 0

    # 15분 버킷 (중복 방지)
    bucket = (now_ms // (15 * 60 * 1000)) * (15 * 60 * 1000)
    try:
        await conn.execute(
            """INSERT OR REPLACE INTO equity_snapshots
               (follower_address, equity_usdc, realized_pnl_cum, unrealized_pnl, open_positions, snapshot_at)
               VALUES (?,?,?,?,?,?)""",
            (follower, equity_usdc, cum_realized, unrealized, open_pos, bucket)
        )
        await conn.commit()
    except Exception as e:
        logger.debug(f"스냅샷 저장 스킵: {e}")


# ── 일별 집계 ────────────────────────────────────────────────
async def aggregate_daily(conn: aiosqlite.Connection, follower: str):
    """오늘 KST 기준 daily_stats 집계"""
    from datetime import date
    today_kst = date.today().isoformat()  # KST (서버 TZ=Asia/Seoul)

    # 오늘의 pnl_records
    midnight_kst_ms = int(datetime.strptime(today_kst, "%Y-%m-%d").replace(
        tzinfo=timezone.utc
    ).timestamp() * 1000) - 9 * 3600 * 1000  # KST offset

    async with conn.execute(
        """SELECT net_pnl, gross_pnl, fee_usdc+builder_fee_usdc as fee
           FROM pnl_records
           WHERE follower_address=? AND closed_at >= ?""",
        (follower, midnight_kst_ms)
    ) as cur:
        records = [dict(r) for r in await cur.fetchall()]

    if not records:
        return

    wins  = sum(1 for r in records if r["net_pnl"] > 0)
    total = len(records)
    daily_pnl = sum(r["net_pnl"] for r in records)
    total_fee = sum(r["fee"] for r in records)

    # 시작/끝 자산 (스냅샷에서)
    async with conn.execute(
        """SELECT equity_usdc FROM equity_snapshots
           WHERE follower_address=? AND snapshot_at >= ?
           ORDER BY snapshot_at ASC LIMIT 1""",
        (follower, midnight_kst_ms)
    ) as cur:
        row = await cur.fetchone()
    start_eq = float(row[0]) if row else 0.0

    async with conn.execute(
        "SELECT equity_usdc FROM equity_snapshots WHERE follower_address=? ORDER BY snapshot_at DESC LIMIT 1",
        (follower,)
    ) as cur:
        row = await cur.fetchone()
    end_eq = float(row[0]) if row else start_eq

    roi_pct = (daily_pnl / start_eq * 100) if start_eq > 0 else 0.0
    wr = (wins / total * 100) if total > 0 else 0.0

    await conn.execute(
        """INSERT OR REPLACE INTO daily_stats
           (follower_address, date_kst, starting_equity, ending_equity,
            daily_pnl, daily_roi_pct, win_trades, lose_trades, total_trades,
            win_rate, total_fee_usdc)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (follower, today_kst, start_eq, end_eq,
         daily_pnl, roi_pct, wins, total - wins, total, wr, total_fee)
    )
    await conn.commit()


# ── 성과 조회 ────────────────────────────────────────────────
async def get_performance_summary(conn: aiosqlite.Connection, follower: str) -> dict:
    """팔로워 전체 성과 요약"""

    # 누적 실현 PnL
    async with conn.execute(
        """SELECT COUNT(*) cnt,
                  COALESCE(SUM(net_pnl),0) total_net,
                  COALESCE(SUM(CASE WHEN net_pnl>0 THEN 1 ELSE 0 END),0) wins,
                  COALESCE(AVG(hold_duration_sec),0) avg_hold_sec,
                  COALESCE(MAX(net_pnl),0) best_trade,
                  COALESCE(MIN(net_pnl),0) worst_trade,
                  COALESCE(SUM(fee_usdc+builder_fee_usdc),0) total_fee
           FROM pnl_records WHERE follower_address=?""",
        (follower,)
    ) as cur:
        row = dict(await cur.fetchone())

    # 미실현 PnL
    async with conn.execute(
        "SELECT COALESCE(SUM(unrealized_pnl),0) FROM positions WHERE follower_address=? AND status='open'",
        (follower,)
    ) as cur:
        unrealized = float((await cur.fetchone())[0])

    # 일별 ROI (Sharpe 계산용)
    async with conn.execute(
        "SELECT daily_roi_pct FROM daily_stats WHERE follower_address=? ORDER BY date_kst DESC LIMIT 30",
        (follower,)
    ) as cur:
        daily_rois = [float(r[0]) for r in await cur.fetchall()]

    sharpe = _calc_sharpe(daily_rois)
    mdd = await _calc_mdd(conn, follower)

    return {
        "total_trades": row["cnt"],
        "wins": row["wins"],
        "losses": row["cnt"] - row["wins"],
        "win_rate_pct": round(row["wins"] / row["cnt"] * 100, 1) if row["cnt"] > 0 else 0,
        "realized_pnl": round(row["total_net"], 4),
        "unrealized_pnl": round(unrealized, 4),
        "total_pnl": round(row["total_net"] + unrealized, 4),
        "best_trade": round(row["best_trade"], 4),
        "worst_trade": round(row["worst_trade"], 4),
        "avg_hold_min": round(row["avg_hold_sec"] / 60, 1) if row["avg_hold_sec"] else 0,
        "total_fee_usdc": round(row["total_fee"], 4),
        "sharpe_30d": round(sharpe, 3),
        "max_drawdown_pct": round(mdd, 2),
    }


async def get_pnl_history(conn: aiosqlite.Connection, follower: str, limit: int = 50) -> list:
    """최근 실현 PnL 이력"""
    async with conn.execute(
        """SELECT symbol, direction, size, entry_price, exit_price,
                  gross_pnl, net_pnl, roi_pct, hold_duration_sec, closed_at
           FROM pnl_records WHERE follower_address=?
           ORDER BY closed_at DESC LIMIT ?""",
        (follower, limit)
    ) as cur:
        rows = await cur.fetchall()
    result = []
    for r in rows:
        r = dict(r)
        r["closed_at_kst"] = datetime.fromtimestamp(r["closed_at"] / 1000).strftime("%Y-%m-%d %H:%M:%S")
        r["hold_min"] = round(r["hold_duration_sec"] / 60, 1) if r["hold_duration_sec"] else 0
        result.append(r)
    return result


async def get_equity_chart(conn: aiosqlite.Connection, follower: str, days: int = 7) -> list:
    """자산 추이 차트 데이터"""
    since_ms = int(time.time() * 1000) - days * 86400 * 1000
    async with conn.execute(
        """SELECT snapshot_at, equity_usdc, realized_pnl_cum, unrealized_pnl, open_positions
           FROM equity_snapshots WHERE follower_address=? AND snapshot_at >= ?
           ORDER BY snapshot_at ASC""",
        (follower, since_ms)
    ) as cur:
        rows = await cur.fetchall()
    result = []
    for r in rows:
        r = dict(r)
        r["time_kst"] = datetime.fromtimestamp(r["snapshot_at"] / 1000).strftime("%m/%d %H:%M")
        result.append(r)
    return result


# ── 헬퍼 ────────────────────────────────────────────────────
def _calc_pnl(side: str, entry: float, exit_p: float, size: float) -> float:
    """LONG(bid): (exit-entry)*size  SHORT(ask): (entry-exit)*size"""
    if side == "bid":   # LONG
        return (exit_p - entry) * size
    else:               # SHORT
        return (entry - exit_p) * size


async def _record_pnl(conn, follower, trader, symbol, side, entry, exit_p,
                      size, gross, fee, builder_fee, open_id, close_id, opened_at, closed_at):
    import uuid
    direction = "long" if side == "bid" else "short"
    net = gross - fee - builder_fee
    cost = entry * size
    roi = net / cost * 100 if cost > 0 else 0.0
    hold = int((closed_at - opened_at) / 1000)

    await conn.execute(
        """INSERT INTO pnl_records
           (id, follower_address, trader_address, symbol, direction,
            open_trade_id, close_trade_id, size, entry_price, exit_price,
            gross_pnl, fee_usdc, builder_fee_usdc, net_pnl, roi_pct,
            hold_duration_sec, opened_at, closed_at, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()), follower, trader, symbol, direction,
         open_id, close_id, size, entry, exit_p,
         gross, fee, builder_fee, net, roi,
         hold, opened_at, closed_at, int(time.time() * 1000))
    )
    logger.debug(f"[PnL] 기록: {symbol} {direction} net={net:+.4f} roi={roi:+.2f}%")


def _calc_sharpe(daily_rois: list) -> float:
    if len(daily_rois) < 5:
        return 0.0
    import math
    mean = sum(daily_rois) / len(daily_rois)
    std = math.sqrt(sum((r - mean)**2 for r in daily_rois) / len(daily_rois))
    if std < 1e-9:
        return 0.0
    return (mean / std) * math.sqrt(252)


async def _calc_mdd(conn, follower) -> float:
    async with conn.execute(
        "SELECT equity_usdc FROM equity_snapshots WHERE follower_address=? ORDER BY snapshot_at ASC",
        (follower,)
    ) as cur:
        rows = [float(r[0]) for r in await cur.fetchall()]
    if not rows:
        return 0.0
    peak = rows[0]
    mdd = 0.0
    for eq in rows:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100 if peak > 0 else 0
        if dd > mdd:
            mdd = dd
    return mdd
