"""
DB 모델 — aiosqlite (SQLite 비동기)
"""
import aiosqlite
import os

DB_PATH = os.getenv("DB_PATH", "copy_perp.db")

CREATE_TABLES = """
-- 팔로워 일별 PnL 스냅샷
CREATE TABLE IF NOT EXISTS follower_pnl_daily (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    follower_address TEXT NOT NULL,
    date            TEXT NOT NULL,
    realized_pnl    REAL DEFAULT 0,
    unrealized_pnl  REAL DEFAULT 0,
    cumulative_pnl  REAL DEFAULT 0,
    trade_count     INTEGER DEFAULT 0,
    win_count       INTEGER DEFAULT 0,
    loss_count      INTEGER DEFAULT 0,
    volume_usdc     REAL DEFAULT 0,
    synced_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(follower_address, date)
);

-- 트레이더별 복사 실적 집계
CREATE TABLE IF NOT EXISTS follower_trader_stats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    follower_address TEXT NOT NULL,
    trader_address  TEXT NOT NULL,
    total_pnl       REAL DEFAULT 0,
    win_count       INTEGER DEFAULT 0,
    loss_count      INTEGER DEFAULT 0,
    total_trades    INTEGER DEFAULT 0,
    total_volume    REAL DEFAULT 0,
    last_trade_at   DATETIME,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(follower_address, trader_address)
);

CREATE TABLE IF NOT EXISTS traders (
    id              TEXT PRIMARY KEY,
    alias           TEXT,
    builder_code    TEXT,
    total_pnl       REAL DEFAULT 0,
    win_rate        REAL DEFAULT 0,
    follower_count  INTEGER DEFAULT 0,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- equity_daily: 트레이더 일별 스냅샷 (Sharpe 정밀 계산용)
-- QA 권고 2026-03-16: Pacifica API가 당일 데이터만 반환하므로
-- 3분 sync 때마다 당일 스냅샷을 여기에 누적 → 7~14일 후 실측 Sharpe 가능
CREATE TABLE IF NOT EXISTS equity_daily (
    address         TEXT NOT NULL,
    date            TEXT NOT NULL,          -- 'YYYY-MM-DD' (UTC)
    equity          REAL DEFAULT 0,         -- 당일 자본 (USD)
    pnl_1d          REAL DEFAULT 0,         -- 당일 PNL (pnl_1d from leaderboard)
    pnl_7d          REAL DEFAULT 0,         -- 7일 PNL (서버 집계)
    pnl_30d         REAL DEFAULT 0,         -- 30일 PNL (서버 집계)
    oi_current      REAL DEFAULT 0,         -- 현재 OI
    synced_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (address, date)
);

CREATE TABLE IF NOT EXISTS followers (
    id                  TEXT PRIMARY KEY,
    trader_id           TEXT REFERENCES traders(id),
    private_key         TEXT,
    copy_ratio          REAL DEFAULT 1.0,
    max_position_usd    REAL DEFAULT 100,
    is_active           INTEGER DEFAULT 1,
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS copy_trades (
    id                  TEXT PRIMARY KEY,
    trader_id           TEXT,
    follower_id         TEXT,
    symbol              TEXT,
    side                TEXT,
    trader_amount       REAL,
    follower_amount     REAL,
    status              TEXT DEFAULT 'pending',
    order_id            TEXT,
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


class DB:
    def __init__(self, path: str = DB_PATH):
        self.path = path

    async def init(self):
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(CREATE_TABLES)
            await db.commit()
            # 마이그레이션: copy_trades 컬럼 추가 (이미 있으면 무시)
            alter_stmts = [
                "ALTER TABLE copy_trades ADD COLUMN pnl REAL",
                "ALTER TABLE copy_trades ADD COLUMN entry_price REAL",
                "ALTER TABLE copy_trades ADD COLUMN exec_price REAL",
                "ALTER TABLE copy_trades ADD COLUMN error_msg TEXT",
                "ALTER TABLE copy_trades ADD COLUMN price TEXT",
                "ALTER TABLE copy_trades ADD COLUMN amount TEXT",
                "ALTER TABLE copy_trades ADD COLUMN trader_address TEXT",
                "ALTER TABLE copy_trades ADD COLUMN follower_address TEXT",
            ]
            for stmt in alter_stmts:
                try:
                    await db.execute(stmt)
                except Exception:
                    pass
            await db.commit()

    async def add_trader(self, trader_id: str, alias: str = ""):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO traders (id, alias) VALUES (?, ?)",
                (trader_id, alias)
            )
            await db.commit()

    async def add_follower(self, follower_id: str, trader_id: str,
                           private_key: str, copy_ratio: float = 1.0,
                           max_position_usd: float = 100):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO followers
                (id, trader_id, private_key, copy_ratio, max_position_usd)
                VALUES (?, ?, ?, ?, ?)
            """, (follower_id, trader_id, private_key, copy_ratio, max_position_usd))
            await db.commit()

    async def get_active_followers(self, trader_id: str) -> list:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM followers WHERE trader_id=? AND is_active=1",
                (trader_id,)
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def save_copy_trade(self, trade: dict):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                INSERT INTO copy_trades
                (id, trader_id, follower_id, symbol, side,
                 trader_amount, follower_amount, status, order_id)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                trade["id"], trade["trader_id"], trade["follower_id"],
                trade["symbol"], trade["side"],
                trade["trader_amount"], trade["follower_amount"],
                trade.get("status", "pending"), trade.get("order_id"),
            ))
            await db.commit()

    async def get_copy_trades(self, limit: int = 50) -> list:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM copy_trades ORDER BY created_at DESC LIMIT ?", (limit,)
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_copy_trades_by_trader(self, trader_id: str) -> list:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM copy_trades WHERE trader_id=? ORDER BY created_at DESC",
                (trader_id,)
            )
            return [dict(r) for r in await cur.fetchall()]

    async def get_copy_trades_by_follower(self, follower_id: str) -> list:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM copy_trades WHERE follower_id=? ORDER BY created_at DESC",
                (follower_id,)
            )
            return [dict(r) for r in await cur.fetchall()]

    # ── equity_daily 스냅샷 (QA 권고 2026-03-16) ──────────────────────
    async def snapshot_equity_daily(self, traders: list[dict]):
        """
        매일 00:00 UTC (또는 3분 sync 시) 트레이더 equity 스냅샷 저장
        INSERT OR REPLACE → 동일 (address, date)는 덮어쓰기

        traders: leaderboard API 응답 목록
          [{address, equity_current, pnl_1d, pnl_7d, pnl_30d, oi_current}]
        """
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        async with aiosqlite.connect(self.path) as db:
            for t in traders:
                addr = t.get("address", "")
                if not addr:
                    continue
                await db.execute("""
                    INSERT OR REPLACE INTO equity_daily
                    (address, date, equity, pnl_1d, pnl_7d, pnl_30d, oi_current)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    addr, today,
                    float(t.get("equity_current") or 0),
                    float(t.get("pnl_1d")         or 0),
                    float(t.get("pnl_7d")          or 0),
                    float(t.get("pnl_30d")         or 0),
                    float(t.get("oi_current")      or 0),
                ))
            await db.commit()

    async def get_equity_history(self, address: str, days: int = 30) -> list:
        """
        트레이더 equity 이력 조회 (Sharpe 계산용)
        Returns: [{date, equity, pnl_1d, ...}] (최신 순)
        """
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("""
                SELECT * FROM equity_daily
                WHERE address = ?
                ORDER BY date DESC
                LIMIT ?
            """, (address, days))
            return [dict(r) for r in await cur.fetchall()]

    async def calc_sharpe_from_history(self, address: str) -> float:
        """
        equity_daily 이력으로 실측 Sharpe 계산
        최소 5일 이력 필요 (그 미만이면 방법 2 추정값 반환)
        """
        import math
        history = await self.get_equity_history(address, days=30)
        if len(history) < 5:
            return 0.0  # 호출자가 방법 2 추정값으로 대체

        # pnl_1d 기반 일별 수익률 (equity 대비)
        daily_returns = []
        for h in history:
            eq  = float(h.get("equity", 0))
            p1d = float(h.get("pnl_1d", 0))
            if eq > 0 and p1d != 0:
                daily_returns.append(p1d / eq)

        if len(daily_returns) < 3:
            return 0.0

        import numpy as np
        arr = np.array(daily_returns)
        std = np.std(arr)
        if std < 1e-10:
            return 0.0
        return float(np.mean(arr) / std * math.sqrt(252))

    async def get_follower_pnl_summary(self, follower_address: str, days: int = 30) -> dict:
        """팔로워의 최근 N일 실현 PnL 합계, 승률, 거래 수, 볼륨 반환"""
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("""
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
            """, (follower_address, cutoff))
            row = await cur.fetchone()
            if row is None:
                return {"total_pnl": 0.0, "win_rate": 0.0, "total_trades": 0,
                        "win_count": 0, "loss_count": 0, "volume_usdc": 0.0}
            r = dict(row)
            total = r["total_trades"] or 0
            wins  = r["win_count"] or 0
            win_rate = wins / total if total > 0 else 0.0
            return {
                "total_pnl":   float(r["total_pnl"] or 0),
                "win_rate":    round(win_rate, 4),
                "total_trades": int(total),
                "win_count":   int(wins),
                "loss_count":  int(r["loss_count"] or 0),
                "volume_usdc": float(r["volume_usdc"] or 0),
            }

    async def get_follower_pnl_by_trader(self, follower_address: str) -> list:
        """팔로워가 각 트레이더별로 얻은 PnL 집계 반환"""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("""
                SELECT
                    trader_address,
                    COALESCE(SUM(CASE WHEN pnl IS NOT NULL THEN pnl ELSE 0 END), 0) as total_pnl,
                    COUNT(*) as trades,
                    SUM(CASE WHEN pnl IS NOT NULL AND pnl > 0 THEN 1 ELSE 0 END) as win_count
                FROM copy_trades
                WHERE follower_address = ? AND status = 'filled'
                GROUP BY trader_address
            """, (follower_address,))
            rows = await cur.fetchall()
            result = []
            for row in rows:
                r = dict(row)
                total = r["trades"] or 0
                wins  = r["win_count"] or 0
                win_rate = wins / total if total > 0 else 0.0
                result.append({
                    "trader_address": r["trader_address"],
                    "total_pnl":     float(r["total_pnl"] or 0),
                    "trades":        int(total),
                    "win_rate":      round(win_rate, 4),
                })
            return result

    async def get_follower_pnl_history(self, follower_address: str, days: int = 30) -> list:
        """일별 PnL 이력 반환 (follower_pnl_daily 우선, 없으면 copy_trades 집계)"""
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            # 먼저 follower_pnl_daily 시도
            cur = await db.execute("""
                SELECT date, realized_pnl, cumulative_pnl, trade_count, win_count
                FROM follower_pnl_daily
                WHERE follower_address = ? AND date >= ?
                ORDER BY date ASC
            """, (follower_address, cutoff))
            rows = await cur.fetchall()
            if rows:
                return [dict(r) for r in rows]

            # fallback: copy_trades 일별 집계
            cur = await db.execute("""
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
            """, (follower_address, cutoff))
            rows = await cur.fetchall()
            # cumulative_pnl 계산
            result = []
            cumulative = 0.0
            for row in rows:
                r = dict(row)
                cumulative += float(r.get("realized_pnl") or 0)
                result.append({
                    "date":          r["date"],
                    "realized_pnl":  float(r.get("realized_pnl") or 0),
                    "cumulative_pnl": round(cumulative, 4),
                    "trade_count":   int(r.get("trade_count") or 0),
                    "win_count":     int(r.get("win_count") or 0),
                })
            return result

    async def snapshot_follower_pnl(self, follower_address: str, date: str = None):
        """오늘(UTC) copy_trades 집계 → follower_pnl_daily UPSERT"""
        from datetime import datetime, timezone
        if date is None:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("""
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
            """, (follower_address, date))
            row = await cur.fetchone()
            if row is None:
                return
            r = dict(row)

            # 누적 PnL 계산
            cur2 = await db.execute("""
                SELECT COALESCE(SUM(realized_pnl), 0) as cum
                FROM follower_pnl_daily
                WHERE follower_address = ? AND date < ?
            """, (follower_address, date))
            prev_row = await cur2.fetchone()
            prev_cum = float(dict(prev_row)["cum"] or 0) if prev_row else 0.0
            cumulative = prev_cum + float(r["realized_pnl"] or 0)

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
                follower_address, date,
                float(r["realized_pnl"] or 0),
                round(cumulative, 4),
                int(r["trade_count"] or 0),
                int(r["win_count"] or 0),
                int(r["loss_count"] or 0),
                float(r["volume_usdc"] or 0),
            ))
            await db.commit()

    async def upsert_follower_trader_stats(
        self,
        follower_address: str,
        trader_address: str,
        pnl_delta: float,
        is_win: bool,
        volume_usdc: float,
    ):
        """follower_trader_stats UPSERT (기존 레코드면 합산, 없으면 insert)"""
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                INSERT INTO follower_trader_stats
                    (follower_address, trader_address, total_pnl,
                     win_count, loss_count, total_trades, total_volume, last_trade_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(follower_address, trader_address) DO UPDATE SET
                    total_pnl    = total_pnl + excluded.total_pnl,
                    win_count    = win_count + excluded.win_count,
                    loss_count   = loss_count + excluded.loss_count,
                    total_trades = total_trades + 1,
                    total_volume = total_volume + excluded.total_volume,
                    last_trade_at = CURRENT_TIMESTAMP,
                    updated_at   = CURRENT_TIMESTAMP
            """, (
                follower_address, trader_address,
                pnl_delta,
                1 if is_win else 0,
                0 if is_win else 1,
                volume_usdc,
            ))
            await db.commit()

    async def get_trader_leaderboard(self, limit: int = 20) -> list:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("""
                SELECT t.id, t.alias, t.follower_count,
                       COUNT(ct.id) as total_copies,
                       SUM(ct.follower_amount) as total_volume
                FROM traders t
                LEFT JOIN copy_trades ct ON t.id = ct.trader_id AND ct.status='filled'
                GROUP BY t.id
                ORDER BY t.follower_count DESC, total_volume DESC
                LIMIT ?
            """, (limit,))
            return [dict(r) for r in await cur.fetchall()]
