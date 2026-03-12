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
