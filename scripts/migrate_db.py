"""
scripts/migrate_db.py
DB 마이그레이션 — 최초 배포 또는 스키마 변경 시 실행
Usage: python3 scripts/migrate_db.py [--db copy_perp.db]

특성:
- 최초 실행 시 전체 스키마 생성
- 기존 DB가 있으면 누락 컬럼만 추가 (idempotent)
- db_version 테이블로 스키마 버전 관리
"""

import sqlite3
import sys
import os
import argparse
import time

# ── 스키마 정의 ──────────────────────────────────────────────────────────────

# 각 migration은 (version, [SQL 리스트]) 형태
MIGRATIONS = [
    # v1: 기본 스키마 + db_version 테이블
    (1, [
        """CREATE TABLE IF NOT EXISTS db_version (
            version     INTEGER PRIMARY KEY,
            applied_at  TEXT DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS traders (
            address      TEXT PRIMARY KEY,
            alias        TEXT,
            win_rate     REAL DEFAULT 0,
            win_count    INTEGER DEFAULT 0,
            lose_count   INTEGER DEFAULT 0,
            last_synced  INTEGER DEFAULT 0,
            total_pnl    REAL DEFAULT 0,
            followers    INTEGER DEFAULT 0,
            active       INTEGER DEFAULT 1,
            created_at   INTEGER,
            pnl_1d       REAL DEFAULT 0,
            pnl_7d       REAL DEFAULT 0,
            pnl_30d      REAL DEFAULT 0,
            pnl_all_time REAL DEFAULT 0,
            equity       REAL DEFAULT 0,
            oi           REAL DEFAULT 0,
            volume_7d    REAL DEFAULT 0,
            volume_30d   REAL DEFAULT 0,
            oi_current   REAL DEFAULT 0,
            roi_30d      REAL DEFAULT 0,
            sharpe       REAL DEFAULT 0,
            tier         INTEGER DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS followers (
            address               TEXT PRIMARY KEY,
            trader_address        TEXT REFERENCES traders(address),
            copy_ratio            REAL DEFAULT 1.0,
            max_position_usdc     REAL DEFAULT 100,
            builder_approved      INTEGER DEFAULT 0,
            builder_code_approved INTEGER DEFAULT 0,
            active                INTEGER DEFAULT 1,
            created_at            INTEGER
        )""",
        """CREATE TABLE IF NOT EXISTS copy_trades (
            id                TEXT PRIMARY KEY,
            follower_address  TEXT REFERENCES followers(address),
            trader_address    TEXT REFERENCES traders(address),
            symbol            TEXT,
            side              TEXT,
            amount            TEXT,
            price             TEXT,
            client_order_id   TEXT UNIQUE,
            status            TEXT DEFAULT 'pending',
            pnl               REAL,
            entry_price       REAL,
            exec_price        REAL,
            created_at        INTEGER,
            filled_at         INTEGER
        )""",
        """CREATE TABLE IF NOT EXISTS fee_records (
            id           TEXT PRIMARY KEY,
            trade_id     TEXT REFERENCES copy_trades(id),
            builder_code TEXT,
            fee_usdc     REAL,
            created_at   INTEGER
        )""",
    ]),

    # v2: 컬럼 추가 예시 (필요 시 여기에 추가)
    # (2, [
    #     "ALTER TABLE traders ADD COLUMN score REAL DEFAULT 0",
    # ]),
]


def get_column_names(conn: sqlite3.Connection, table: str) -> set:
    """테이블의 현재 컬럼 이름 집합 반환
    SECURITY: table명은 반드시 내부 상수여야 함 — 외부 입력으로 받지 말 것.
    PRAGMA는 파라미터 바인딩 미지원 → 허용 테이블 목록으로 화이트리스트 방어.
    """
    _ALLOWED_TABLES = frozenset({
        "traders", "followers", "copy_trades", "fee_records",
        "follower_positions", "mainnet_stats", "crs_snapshots",
        "db_version", "pnl_records", "positions",
    })
    if table not in _ALLOWED_TABLES:
        raise ValueError(f"get_column_names: 허용되지 않은 테이블명: {table!r}")
    try:
        cursor = conn.execute(f"PRAGMA table_info({table})")  # noqa: S608 — whitelist validated
        return {row[1] for row in cursor.fetchall()}
    except Exception:
        return set()


def get_current_version(conn: sqlite3.Connection) -> int:
    """db_version 테이블에서 현재 버전 조회. 없으면 0 반환"""
    try:
        row = conn.execute("SELECT MAX(version) FROM db_version").fetchone()
        return row[0] if row and row[0] is not None else 0
    except Exception:
        return 0


def run_sql_safe(conn: sqlite3.Connection, sql: str) -> bool:
    """SQL 실행. 컬럼 중복 에러(duplicate column)는 무시하고 idempotent 유지"""
    sql_upper = sql.strip().upper()
    try:
        conn.execute(sql)
        return True
    except sqlite3.OperationalError as e:
        err = str(e).lower()
        if "duplicate column" in err:
            # ALTER TABLE ADD COLUMN 중복: 무시 (idempotent)
            print(f"  [skip] 이미 존재하는 컬럼: {e}")
            return True
        raise


def migrate(db_path: str, dry_run: bool = False) -> None:
    print(f"🗄️  DB 마이그레이션 시작: {db_path}")
    if not os.path.exists(db_path):
        print(f"  [info] DB 파일 없음 → 신규 생성")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    try:
        current_ver = get_current_version(conn)
        print(f"  현재 DB 버전: v{current_ver}")

        applied = 0
        for version, statements in MIGRATIONS:
            if version <= current_ver:
                print(f"  [skip] v{version} — 이미 적용됨")
                continue

            print(f"  → v{version} 마이그레이션 적용 중...")
            if not dry_run:
                for sql in statements:
                    preview = sql.strip().split("\n")[0][:80]
                    print(f"    SQL: {preview}...")
                    run_sql_safe(conn, sql)

                # 버전 기록
                conn.execute(
                    "INSERT OR REPLACE INTO db_version(version, applied_at) VALUES(?, ?)",
                    (version, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
                )
                conn.commit()
                print(f"  ✅ v{version} 완료")
                applied += 1
            else:
                print(f"    [dry-run] {len(statements)}개 SQL 건너뜀")
                applied += 1

        if applied == 0:
            print("  ✅ 이미 최신 버전입니다.")
        else:
            new_ver = get_current_version(conn)
            print(f"  ✅ 마이그레이션 완료: v{current_ver} → v{new_ver} ({applied}개 적용)")

        # 현재 테이블 목록 출력
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        print(f"  📋 테이블 목록: {[t[0] for t in tables]}")

    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Copy Perp DB 마이그레이션")
    parser.add_argument("--db", default=os.getenv("DB_PATH", "copy_perp.db"), help="DB 파일 경로")
    parser.add_argument("--dry-run", action="store_true", help="실제 변경 없이 시뮬레이션")
    args = parser.parse_args()

    try:
        migrate(args.db, dry_run=args.dry_run)
        sys.exit(0)
    except Exception as e:
        print(f"❌ 마이그레이션 실패: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
