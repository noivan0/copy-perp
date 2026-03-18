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
    win_count   INTEGER DEFAULT 0,
    lose_count  INTEGER DEFAULT 0,
    last_synced INTEGER DEFAULT 0,
    total_pnl   REAL DEFAULT 0,
    followers   INTEGER DEFAULT 0,
    active      INTEGER DEFAULT 1,
    created_at  INTEGER,
    pnl_1d      REAL DEFAULT 0,
    pnl_7d      REAL DEFAULT 0,
    pnl_30d     REAL DEFAULT 0,
    pnl_all_time REAL DEFAULT 0,
    equity      REAL DEFAULT 0,
    oi          REAL DEFAULT 0,
    volume_7d   REAL DEFAULT 0,
    volume_30d  REAL DEFAULT 0,
    oi_current  REAL DEFAULT 0,
    roi_30d     REAL DEFAULT 0,
    sharpe      REAL DEFAULT 0,
    tier        INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS followers (
    address                 TEXT PRIMARY KEY,
    trader_address          TEXT REFERENCES traders(address),
    copy_ratio              REAL DEFAULT 1.0,   -- 팔로워 자금 대비 복사 비율
    max_position_usdc       REAL DEFAULT 100,   -- 포지션당 최대 금액
    builder_approved        INTEGER DEFAULT 0,  -- 구 컬럼 (하위 호환)
    builder_code_approved   INTEGER DEFAULT 0,  -- Builder Code 승인 여부 (noivan)
    active                  INTEGER DEFAULT 1,
    created_at              INTEGER
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
    entry_price         REAL,   -- 진입가 (청산 PnL 계산용)
    exec_price          REAL,   -- 체결가 (실제 주문 체결 가격)
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
    # WAL 모드: 동시 읽기/쓰기 성능 개선
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.executescript(CREATE_SQL)
    # 마이그레이션: 기존 DB에 누락된 컬럼 추가
    _migrations = [
        # traders 컬럼
        "ALTER TABLE traders ADD COLUMN win_count INTEGER DEFAULT 0",
        "ALTER TABLE traders ADD COLUMN lose_count INTEGER DEFAULT 0",
        "ALTER TABLE traders ADD COLUMN last_synced INTEGER DEFAULT 0",
        "ALTER TABLE traders ADD COLUMN tier TEXT DEFAULT 'C'",
        "ALTER TABLE traders ADD COLUMN sharpe REAL DEFAULT 0",
        # followers 컬럼
        "ALTER TABLE followers ADD COLUMN builder_code_approved INTEGER DEFAULT 0",
        "ALTER TABLE followers ADD COLUMN privy_user_id TEXT",
        # copy_trades 컬럼
        "ALTER TABLE copy_trades ADD COLUMN error_msg TEXT",
        "ALTER TABLE copy_trades ADD COLUMN entry_price REAL",
        "ALTER TABLE copy_trades ADD COLUMN exec_price REAL",
        # fee_records 테이블 (없으면 CREATE, 있으면 무시됨 — executescript 특성)
        # fee_records는 CREATE_SQL에 이미 포함되어 있음
    ]
    for sql in _migrations:
        try:
            await conn.execute(sql)
        except Exception:
            pass  # 이미 컬럼 있으면 무시

    # 인덱스 추가 (없으면 생성)
    _indexes = [
        "CREATE INDEX IF NOT EXISTS idx_copy_trades_follower ON copy_trades(follower_address)",
        "CREATE INDEX IF NOT EXISTS idx_copy_trades_created_at ON copy_trades(created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_copy_trades_status ON copy_trades(status)",
        "CREATE INDEX IF NOT EXISTS idx_followers_active ON followers(active)",
        "CREATE INDEX IF NOT EXISTS idx_followers_trader ON followers(trader_address)",
        "CREATE INDEX IF NOT EXISTS idx_traders_active ON traders(active)",
    ]
    for sql in _indexes:
        try:
            await conn.execute(sql)
        except Exception:
            pass

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
           (address, trader_address, copy_ratio, max_position_usdc,
            builder_approved, builder_code_approved, active, created_at)
           VALUES (?, ?, ?, ?, 1, 1, 1, ?)""",
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
            client_order_id, status, pnl, entry_price, exec_price, created_at, error_msg)
           VALUES (:id, :follower_address, :trader_address, :symbol, :side,
                   :amount, :price, :client_order_id, :status, :pnl,
                   :entry_price, :exec_price, :created_at, :error_msg)""",
        {
            **trade,
            "pnl": trade.get("pnl"),
            "entry_price": trade.get("entry_price"),
            "exec_price": trade.get("exec_price"),
            "error_msg": trade.get("error_msg"),
        }
    )
    await conn.commit()


async def get_leaderboard(conn, limit: int = 20) -> list:
    """복합 점수 기준 정렬: roi_30d*0.6 + roi_7d*0.3 + (1d 양수 보너스)
    전략팀 분석 기준: ROI 60% + 일관성 40%

    수정 이력:
    - 2025-03-16: roi_30d, roi_7d, roi_1d, score, active, profit_factor 필드 추가
                  (프론트엔드 Leaderboard 컴포넌트 요구 필드 충족)
    """
    # pnl_1d/equity 컬럼이 없는 구형 DB와 호환되는 쿼리
    async with conn.execute(
        """SELECT address, alias, win_rate, total_pnl, followers, active,
                  COALESCE(pnl_1d, 0) as pnl_1d,
                  COALESCE(pnl_7d, 0) as pnl_7d,
                  COALESCE(pnl_30d, 0) as pnl_30d,
                  COALESCE(pnl_all_time, 0) as pnl_all_time,
                  COALESCE(equity, 0) as equity,
                  COALESCE(volume_7d, 0) as volume_7d,
                  COALESCE(volume_30d, 0) as volume_30d,
                  COALESCE(oi_current, 0) as oi_current,
                  COALESCE(win_count, 0) as win_count,
                  COALESCE(lose_count, 0) as lose_count,
                  -- roi_30d: equity 기반 ROI (퍼센트)
                  CASE WHEN COALESCE(equity, 0) > 0
                       THEN ROUND(COALESCE(pnl_30d, 0) / equity * 100, 2)
                       ELSE COALESCE(roi_30d, 0)
                  END AS roi_30d,
                  -- roi_7d
                  CASE WHEN COALESCE(equity, 0) > 0
                       THEN ROUND(COALESCE(pnl_7d, 0) / equity * 100, 2)
                       ELSE 0
                  END AS roi_7d,
                  -- roi_1d
                  CASE WHEN COALESCE(equity, 0) > 0
                       THEN ROUND(COALESCE(pnl_1d, 0) / equity * 100, 2)
                       ELSE 0
                  END AS roi_1d,
                  -- profit_factor: 단순화 (양수 PnL/손실 비율, 데이터 없으면 0)
                  0.0 AS profit_factor,
                  -- score: composite_score (프론트엔드 정렬용)
                  CASE WHEN COALESCE(equity, 0) > 0
                       THEN (COALESCE(pnl_30d,0)/equity)*0.6 + (COALESCE(pnl_7d,0)/equity)*0.3 + (CASE WHEN COALESCE(pnl_1d,0) > 0 THEN 0.1 ELSE 0 END)
                       ELSE total_pnl
                  END AS score
           FROM traders WHERE active = 1
           ORDER BY score DESC LIMIT ?""",
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
