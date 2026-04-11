import logging
logger = logging.getLogger(__name__)
"""
DB 초기화 및 CRUD — SQLite + aiosqlite

테이블:
- traders: 팔로우 대상 트레이더
- followers: Copy Perp 팔로워
- copy_trades: 복사된 주문 기록
- fee_records: Builder Code 수수료 기록
"""

import os
from typing import Optional

# ── DB 백엔드 선택 ──────────────────────────────────────────────────────────
# TURSO_URL 환경변수 있으면 Turso(libSQL 클라우드), 없으면 로컬 SQLite
TURSO_URL   = os.getenv("TURSO_URL", "")
TURSO_TOKEN = os.getenv("TURSO_TOKEN", "")
_USE_TURSO  = bool(TURSO_URL and TURSO_TOKEN)

if _USE_TURSO:
    from db.turso_adapter import TursoConnection as _DbConn
    from db.turso_adapter import TursoConnection
    logger.info(f"[DB] Turso 모드: {TURSO_URL[:50]}")
else:
    import aiosqlite
    _DbConn = None  # aiosqlite 직접 사용
    logger.info("[DB] 로컬 SQLite 모드")

DB_PATH = os.getenv("DB_PATH", "copy_perp.db")

# DB 디렉토리 자동 생성 (로컬 SQLite 전용)
if not _USE_TURSO:
    _db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    if _db_dir and not os.path.exists(_db_dir):
        try:
            os.makedirs(_db_dir, exist_ok=True)
        except OSError:
            DB_PATH = "/tmp/copy_perp.db"

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
    address                 TEXT NOT NULL,
    trader_address          TEXT REFERENCES traders(address),
    copy_ratio              REAL DEFAULT 1.0,
    max_position_usdc       REAL DEFAULT 100,
    builder_approved        INTEGER DEFAULT 0,
    builder_code_approved   INTEGER DEFAULT 0,
    active                  INTEGER DEFAULT 1,
    created_at              INTEGER,
    privy_user_id           TEXT,
    stop_loss_pct           REAL DEFAULT 8.0,
    take_profit_pct         REAL DEFAULT 15.0,
    agent_bound             INTEGER DEFAULT 0,
    PRIMARY KEY (address, trader_address)
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
    status              TEXT DEFAULT 'pending',  -- pending/filled/failed/skipped_insufficient
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

CREATE TABLE IF NOT EXISTS performance_snapshots (
    id              TEXT PRIMARY KEY,
    address         TEXT NOT NULL,
    role            TEXT NOT NULL,
    snapshot_date   TEXT NOT NULL,
    equity          REAL DEFAULT 0,
    daily_pnl       REAL DEFAULT 0,
    daily_roi_pct   REAL DEFAULT 0,
    cum_pnl         REAL DEFAULT 0,
    cum_roi_pct     REAL DEFAULT 0,
    trade_count     INTEGER DEFAULT 0,
    win_count       INTEGER DEFAULT 0,
    loss_count      INTEGER DEFAULT 0,
    created_at      INTEGER,
    UNIQUE(address, snapshot_date)
);
CREATE INDEX IF NOT EXISTS idx_perf_snap_address ON performance_snapshots(address, snapshot_date DESC);

CREATE TABLE IF NOT EXISTS follower_positions (
    follower_address TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    side             TEXT NOT NULL,
    entry_price      REAL NOT NULL,
    size             REAL NOT NULL,
    mark_price       REAL DEFAULT 0,
    unrealized_pnl   REAL DEFAULT 0,
    opened_at        INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    PRIMARY KEY (follower_address, symbol)
);

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

CREATE TABLE IF NOT EXISTS trader_crs_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    address         TEXT NOT NULL,
    alias           TEXT DEFAULT '',
    crs             REAL NOT NULL,
    grade           TEXT NOT NULL,
    momentum_score  REAL DEFAULT 0,
    profitability_score REAL DEFAULT 0,
    risk_score      REAL DEFAULT 0,
    consistency_score REAL DEFAULT 0,
    copyability_score REAL DEFAULT 0,
    computed_at     TEXT NOT NULL DEFAULT (date('now'))
);
CREATE INDEX IF NOT EXISTS idx_crs_history_address ON trader_crs_history(address, computed_at DESC);
CREATE INDEX IF NOT EXISTS idx_crs_history_date ON trader_crs_history(computed_at DESC);
"""


async def init_db(db_path: str = DB_PATH):
    """DB 초기화 — TURSO_URL 있으면 Turso, 없으면 로컬 SQLite"""
    if _USE_TURSO:
        conn = await TursoConnection.connect()
        # Turso는 PRAGMA 불필요 (클라우드 관리형)
    else:
        conn = await aiosqlite.connect(db_path, timeout=30)
        conn.row_factory = aiosqlite.Row
        # WAL 모드: 동시 읽기/쓰기 성능 개선
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA busy_timeout=30000")
    await conn.executescript(CREATE_SQL)
    # 마이그레이션: 기존 DB에 누락된 컬럼 추가
    _migrations = [
        # traders 컬럼
        "ALTER TABLE traders ADD COLUMN win_count INTEGER DEFAULT 0",
        "ALTER TABLE traders ADD COLUMN lose_count INTEGER DEFAULT 0",
        "ALTER TABLE traders ADD COLUMN last_synced INTEGER DEFAULT 0",
        "ALTER TABLE traders ADD COLUMN tier TEXT DEFAULT 'C'",
        "ALTER TABLE traders ADD COLUMN sharpe REAL DEFAULT 0",
        "ALTER TABLE traders ADD COLUMN roi_30d REAL DEFAULT 0",
        "ALTER TABLE traders ADD COLUMN roi_7d REAL DEFAULT 0",
        "ALTER TABLE traders ADD COLUMN profit_factor REAL DEFAULT 0",
        "ALTER TABLE traders ADD COLUMN total_trades INTEGER DEFAULT 0",
        # followers 컬럼
        "ALTER TABLE followers ADD COLUMN builder_code_approved INTEGER DEFAULT 0",
        "ALTER TABLE followers ADD COLUMN privy_user_id TEXT",
        "ALTER TABLE followers ADD COLUMN stop_loss_pct REAL DEFAULT 8.0",
        "ALTER TABLE followers ADD COLUMN take_profit_pct REAL DEFAULT 15.0",
        # copy_trades 컬럼
        "ALTER TABLE copy_trades ADD COLUMN error_msg TEXT",
        "ALTER TABLE copy_trades ADD COLUMN entry_price REAL",
        "ALTER TABLE copy_trades ADD COLUMN exec_price REAL",
        "ALTER TABLE copy_trades ADD COLUMN closed_at INTEGER",
        "ALTER TABLE copy_trades ADD COLUMN hold_duration_sec INTEGER",
        "ALTER TABLE copy_trades ADD COLUMN trader_alias TEXT",
        "ALTER TABLE copy_trades ADD COLUMN fee_usdc REAL DEFAULT 0",
        # follower_positions 컬럼 (Round 6: mark_price, unrealized_pnl 추가)
        "ALTER TABLE follower_positions ADD COLUMN mark_price REAL DEFAULT 0",
        "ALTER TABLE follower_positions ADD COLUMN unrealized_pnl REAL DEFAULT 0",
        # follower_positions 컬럼 (R10: SL/TP 가격 저장 — StopLossMonitor 2차 스캔 정확도)
        "ALTER TABLE follower_positions ADD COLUMN stop_loss_price REAL DEFAULT 0",
        "ALTER TABLE follower_positions ADD COLUMN take_profit_price REAL DEFAULT 0",
        "ALTER TABLE follower_positions ADD COLUMN high_price REAL DEFAULT 0",
        "ALTER TABLE follower_positions ADD COLUMN strategy TEXT DEFAULT 'passive'",
        # fee_records 테이블 (없으면 CREATE, 있으면 무시됨 — executescript 특성)
        # fee_records는 CREATE_SQL에 이미 포함되어 있음
        # Round 8: trader_crs_history 테이블 (기존 DB 마이그레이션용)
        # CREATE_SQL에 이미 포함되어 있으므로 실제 마이그레이션은 executescript가 처리
        # 인덱스만 별도 추가 시도 (이미 있으면 무시됨)
        "CREATE INDEX IF NOT EXISTS idx_crs_history_address ON trader_crs_history(address, computed_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_crs_history_date ON trader_crs_history(computed_at DESC)",
        # AgentBind (multi-user): followers 테이블에 agent_bound 컬럼 추가
        "ALTER TABLE followers ADD COLUMN agent_bound INTEGER DEFAULT 0",
    ]
    import logging as _db_log
    _db_logger = _db_log.getLogger(__name__)
    _mig_applied = 0
    _mig_skipped = 0
    for sql in _migrations:
        try:
            await conn.execute(sql)
            _mig_applied += 1
        except Exception as _mig_e:
            # 이미 컬럼 있으면 "duplicate column name" 오류 → 무시 (정상)
            # 다른 오류도 무시하되 DEBUG 로그 남김
            _db_logger.debug(f"[migrate_db] skipped (already exists?): {sql[:60]!r} → {_mig_e}")
            _mig_skipped += 1
    if _mig_applied > 0:
        _db_logger.info(f"[migrate_db] applied={_mig_applied} skipped={_mig_skipped}")

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

    # PnL Tracker 마이그레이션 (positions, pnl_records, equity_snapshots, daily_stats)
    try:
        from db.pnl_tracker import apply_migrations
        await apply_migrations(conn)
    except Exception as _e:
        import logging as _log
        _log.getLogger(__name__).warning(f"PnL Tracker 마이그레이션 경고: {_e}")

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
    max_position_usdc: float = 100.0,
    stop_loss_pct=None,
    take_profit_pct=None,
) -> None:
    import time
    _now = int(time.time() * 1000)
    # stop_loss_pct/take_profit_pct 컬럼이 없을 수 있으므로 방어적 2단계 INSERT
    try:
        await conn.execute(
            """INSERT OR REPLACE INTO followers
               (address, trader_address, copy_ratio, max_position_usdc,
                builder_approved, builder_code_approved, active, created_at,
                stop_loss_pct, take_profit_pct)
               VALUES (?, ?, ?, ?, 1, 1, 1, ?, ?, ?)""",
            (address, trader_address, copy_ratio, max_position_usdc,
             _now, stop_loss_pct, take_profit_pct)
        )
    except Exception:
        # SL/TP 컬럼 미생성 시 기본 INSERT fallback (migration 전 호환)
        await conn.execute(
            """INSERT OR REPLACE INTO followers
               (address, trader_address, copy_ratio, max_position_usdc,
                builder_approved, builder_code_approved, active, created_at)
               VALUES (?, ?, ?, ?, 1, 1, 1, ?)""",
            (address, trader_address, copy_ratio, max_position_usdc, _now)
        )
    await conn.commit()


async def get_followers(conn, trader_address: str) -> list:
    # R13 P1: Turso(libSQL)에서 boolean을 TRUE/1 혼용 가능 → active != 0으로 안전 처리
    async with conn.execute(
        "SELECT * FROM followers WHERE trader_address = ? AND active != 0",
        (trader_address,)
    ) as cur:
        rows = await cur.fetchall()
        # sqlite3.Row / aiosqlite.Row 모두 dict로 변환 (.get() 안전 보장)
        return [dict(r) for r in rows]


async def record_copy_trade(conn, trade: dict) -> None:
    # 중복 키 방지: **trade 언패킹 없이 명시적 키만 사용
    # P1 Fix (Round 6): fee_usdc 컬럼 추가
    await conn.execute(
        """INSERT OR IGNORE INTO copy_trades
           (id, follower_address, trader_address, symbol, side, amount, price,
            client_order_id, status, pnl, entry_price, exec_price, created_at, error_msg, fee_usdc)
           VALUES (:id, :follower_address, :trader_address, :symbol, :side,
                   :amount, :price, :client_order_id, :status, :pnl,
                   :entry_price, :exec_price, :created_at, :error_msg, :fee_usdc)""",
        {
            "id": trade.get("id"),
            "follower_address": trade.get("follower_address"),
            "trader_address": trade.get("trader_address"),
            "symbol": trade.get("symbol"),
            "side": trade.get("side"),
            "amount": trade.get("amount"),
            "price": trade.get("price"),
            "client_order_id": trade.get("client_order_id"),
            "status": trade.get("status"),
            "pnl": trade.get("pnl"),
            "entry_price": trade.get("entry_price"),
            "exec_price": trade.get("exec_price"),
            "created_at": trade.get("created_at"),
            "error_msg": trade.get("error_msg"),
            "fee_usdc": trade.get("fee_usdc", 0),
        }
    )
    await conn.commit()

    # pnl이 있는 경우 follower_trader_stats 자동 업서트
    pnl = trade.get("pnl")
    if pnl is not None:
        follower_address = trade.get("follower_address")
        trader_address = trade.get("trader_address")
        if follower_address and trader_address:
            try:
                pnl_val = float(pnl)
                is_win = pnl_val > 0
                amount_str = trade.get("amount") or "0"
                price_str = trade.get("price") or "0"
                try:
                    volume = float(amount_str) * float(price_str)
                except (TypeError, ValueError):
                    volume = 0.0
                await conn.execute("""
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
                    pnl_val,
                    1 if is_win else 0,
                    0 if is_win else 1,
                    volume,
                ))
                await conn.commit()
            except Exception:
                pass  # 통계 업데이트 실패는 조용히 무시


async def get_leaderboard(conn, limit: int = 20, offset: int = 0) -> list:
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
           ORDER BY score DESC LIMIT ? OFFSET ?""",
        (limit, offset)
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


# ── follower_positions CRUD ───────────────────────────────────────────────────

async def upsert_follower_position(
    conn,
    follower_address: str,
    symbol: str,
    side: str,
    entry_price: float,
    size: float,
    stop_loss_price: float = 0.0,
    take_profit_price: float = 0.0,
    high_price: float = 0.0,
    strategy: str = "passive",
) -> None:
    """팔로워 포지션 진입/업서트 (DB 영속화)
    R10: stop_loss_price/take_profit_price/high_price/strategy 저장 추가
    → StopLossMonitor 2차 스캔(follower_positions) 시 SL/TP 정확도 확보
    """
    import time as _t
    now = int(_t.time() * 1000)
    hp = high_price if high_price > 0 else entry_price
    await conn.execute(
        """INSERT INTO follower_positions
               (follower_address, symbol, side, entry_price, size,
                stop_loss_price, take_profit_price, high_price, strategy,
                opened_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(follower_address, symbol) DO UPDATE SET
               side=excluded.side,
               entry_price=excluded.entry_price,
               size=excluded.size,
               stop_loss_price=excluded.stop_loss_price,
               take_profit_price=excluded.take_profit_price,
               high_price=excluded.high_price,
               strategy=excluded.strategy,
               updated_at=excluded.updated_at""",
        (follower_address, symbol, side, entry_price, size,
         stop_loss_price, take_profit_price, hp, strategy, now, now),
    )
    await conn.commit()


async def get_follower_position(conn, follower_address: str, symbol: str) -> "dict | None":
    """팔로워의 특정 심볼 포지션 조회. 없으면 None."""
    async with conn.execute(
        "SELECT * FROM follower_positions WHERE follower_address=? AND symbol=?",
        (follower_address, symbol),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def delete_follower_position(conn, follower_address: str, symbol: str) -> None:
    """포지션 청산 시 DB에서 삭제"""
    await conn.execute(
        "DELETE FROM follower_positions WHERE follower_address=? AND symbol=?",
        (follower_address, symbol),
    )
    await conn.commit()


async def get_all_follower_positions(conn, follower_address: str) -> "list[dict]":
    """팔로워의 모든 열린 포지션 목록"""
    async with conn.execute(
        "SELECT * FROM follower_positions WHERE follower_address=?",
        (follower_address,),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ── trader_crs_history CRUD (R8) ─────────────────────────────────────────────

async def save_crs_snapshot(conn, crs_results: list) -> int:
    """
    CRS 점수 스냅샷 저장 (하루 1회 — 이미 오늘 데이터 있으면 스킵)

    crs_results: CRSResult 객체 또는 dict 리스트
    반환: 저장된 행 수
    """
    import time as _t
    today = __import__("datetime").date.today().isoformat()  # YYYY-MM-DD

    # 오늘 이미 저장됐는지 확인
    async with conn.execute(
        "SELECT COUNT(*) FROM trader_crs_history WHERE computed_at=?", (today,)
    ) as cur:
        row = await cur.fetchone()
    existing = row[0] if row else 0
    if existing > 0:
        return 0  # 이미 오늘 스냅샷 존재 → 스킵

    saved = 0
    for r in crs_results:
        # CRSResult 객체 또는 dict 모두 지원
        if hasattr(r, "address"):
            addr   = r.address
            alias  = r.alias
            crs    = r.crs
            grade  = r.grade
            m_s    = r.momentum_score
            p_s    = r.profitability_score
            ri_s   = r.risk_score
            c_s    = r.consistency_score
            cp_s   = r.copyability_score
        else:
            addr   = r.get("address", "")
            alias  = r.get("alias", "")
            crs    = r.get("crs", 0)
            grade  = r.get("grade", "D")
            m_s    = r.get("momentum_score", 0)
            p_s    = r.get("profitability_score", 0)
            ri_s   = r.get("risk_score", 0)
            c_s    = r.get("consistency_score", 0)
            cp_s   = r.get("copyability_score", 0)

        if not addr or crs <= 0:
            continue

        try:
            await conn.execute(
                """INSERT OR IGNORE INTO trader_crs_history
                   (address, alias, crs, grade, momentum_score, profitability_score,
                    risk_score, consistency_score, copyability_score, computed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (addr, alias, crs, grade, m_s, p_s, ri_s, c_s, cp_s, today)
            )
            saved += 1
        except Exception as _e:
            logger.debug(f"crs_snapshot insert skipped: {_e}")

    if saved > 0:
        await conn.commit()
    return saved


async def get_crs_history(conn, address: str, days: int = 30) -> list:
    """특정 트레이더의 CRS 점수 변동 이력 조회"""
    async with conn.execute(
        """SELECT address, alias, crs, grade, momentum_score, profitability_score,
                  risk_score, consistency_score, copyability_score, computed_at
           FROM trader_crs_history
           WHERE address=?
           ORDER BY computed_at DESC
           LIMIT ?""",
        (address, days)
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]
