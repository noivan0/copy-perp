"""
DB 모델 — SQLite (aiosqlite)
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
    copy_ratio          REAL DEFAULT 1.0,
    max_position_usd    REAL DEFAULT 100,
    is_active           INTEGER DEFAULT 1,
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS copy_trades (
    id                  TEXT PRIMARY KEY,
    trader_id           TEXT,
    follower_id         TEXT,
    original_order_id   TEXT,
    copied_order_id     TEXT,
    symbol              TEXT,
    side                TEXT,
    trader_amount       REAL,
    follower_amount     REAL,
    status              TEXT DEFAULT 'pending',
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fee_records (
    id              TEXT PRIMARY KEY,
    trade_id        TEXT REFERENCES copy_trades(id),
    fee_amount      REAL,
    builder_code    TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_TABLES)
        await db.commit()

async def get_active_followers(trader_id: str) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM followers WHERE trader_id=? AND is_active=1",
            (trader_id,)
        )
        return await cursor.fetchall()

async def save_copy_trade(trade: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO copy_trades
            (id, trader_id, follower_id, original_order_id, copied_order_id,
             symbol, side, trader_amount, follower_amount, status)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            trade["id"], trade["trader_id"], trade["follower_id"],
            trade["original_order_id"], trade.get("copied_order_id"),
            trade["symbol"], trade["side"],
            trade["trader_amount"], trade["follower_amount"],
            trade.get("status", "pending")
        ))
        await db.commit()
