"""
DB 초기화 및 CRUD — SQLite + aiosqlite

테이블:
- traders: 팔로우 대상 트레이더
- followers: Copy Perp 팔로워
- copy_trades: 복사된 주문 기록
- fee_records: Builder Code 수수료 기록
"""

import aiosqlite
import os
from typing import Optional

DB_PATH = os.getenv("DB_PATH", "copy_perp.db")

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS traders (
    address     TEXT PRIMARY KEY,
    alias       TEXT,
    win_rate    REAL DEFAULT 0,
    total_pnl   REAL DEFAULT 0,
    followers   INTEGER DEFAULT 0,
    active      INTEGER DEFAULT 1,
    created_at  INTEGER
);

CREATE TABLE IF NOT EXISTS followers (
    address             TEXT PRIMARY KEY,
    trader_address      TEXT REFERENCES traders(address),
    copy_ratio          REAL DEFAULT 1.0,   -- 팔로워 자금 대비 복사 비율
    max_position_usdc   REAL DEFAULT 100,   -- 포지션당 최대 금액
    builder_approved    INTEGER DEFAULT 0,  -- Builder Code 승인 여부
    active              INTEGER DEFAULT 1,
    created_at          INTEGER
);

CREATE TABLE IF NOT EXISTS copy_trades (
    id                  TEXT PRIMARY KEY,
    follower_address    TEXT REFERENCES followers(address),
    trader_address      TEXT REFERENCES traders(address),
    symbol              TEXT,
    side                TEXT,
    amount              TEXT,
    price               TEXT,
    client_order_id     TEXT UNIQUE,
    status              TEXT DEFAULT 'pending',  -- pending/filled/failed
    pnl                 REAL,
    created_at          INTEGER,
    filled_at           INTEGER
);

CREATE TABLE IF NOT EXISTS fee_records (
    id              TEXT PRIMARY KEY,
    trade_id        TEXT REFERENCES copy_trades(id),
    builder_code    TEXT,
    fee_usdc        REAL,
    created_at      INTEGER
);
"""


async def init_db(db_path: str = DB_PATH) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.executescript(CREATE_SQL)
    await conn.commit()
    return conn


async def add_trader(conn, address: str, alias: str = "") -> None:
    import time
    await conn.execute(
        "INSERT OR IGNORE INTO traders (address, alias, created_at) VALUES (?, ?, ?)",
        (address, alias, int(time.time() * 1000))
    )
    await conn.commit()


async def add_follower(
    conn,
    address: str,
    trader_address: str,
    copy_ratio: float = 1.0,
    max_position_usdc: float = 100.0
) -> None:
    import time
    await conn.execute(
        """INSERT OR REPLACE INTO followers
           (address, trader_address, copy_ratio, max_position_usdc, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (address, trader_address, copy_ratio, max_position_usdc, int(time.time() * 1000))
    )
    await conn.commit()


async def get_followers(conn, trader_address: str) -> list:
    async with conn.execute(
        "SELECT * FROM followers WHERE trader_address = ? AND active = 1",
        (trader_address,)
    ) as cur:
        return await cur.fetchall()


async def record_copy_trade(conn, trade: dict) -> None:
    await conn.execute(
        """INSERT OR IGNORE INTO copy_trades
           (id, follower_address, trader_address, symbol, side, amount, price,
            client_order_id, status, created_at)
           VALUES (:id, :follower_address, :trader_address, :symbol, :side,
                   :amount, :price, :client_order_id, :status, :created_at)""",
        trade
    )
    await conn.commit()


async def get_leaderboard(conn, limit: int = 20) -> list:
    async with conn.execute(
        """SELECT address, alias, win_rate, total_pnl, followers
           FROM traders WHERE active = 1
           ORDER BY total_pnl DESC LIMIT ?""",
        (limit,)
    ) as cur:
        return await cur.fetchall()


# ── 테스트 ──────────────────────────────────
if __name__ == "__main__":
    import asyncio

    async def main():
        conn = await init_db(":memory:")
        await add_trader(conn, "3AHZqroc...", "CEO")
        await add_follower(conn, "follower1...", "3AHZqroc...")
        followers = await get_followers(conn, "3AHZqroc...")
        print(f"팔로워: {len(followers)}명")
        for f in followers:
            print(dict(f))
        await conn.close()
        print("✅ DB 정상")

    asyncio.run(main())

async def get_copy_trades(conn, limit: int = 50, follower: str = None) -> list:
    if follower:
        async with conn.execute(
            "SELECT * FROM copy_trades WHERE follower_address=? ORDER BY created_at DESC LIMIT ?",
            (follower, limit)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]
    async with conn.execute(
        "SELECT * FROM copy_trades ORDER BY created_at DESC LIMIT ?", (limit,)
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]
