"""
DB 모델 — aiosqlite (SQLite 비동기)
"""
import aiosqlite
import os

DB_PATH = os.getenv("DB_PATH", "copy_perp.db")

CREATE_TABLES = """
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
