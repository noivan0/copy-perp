"""
db/turso_adapter.py — Turso(libSQL) ↔ aiosqlite 호환 어댑터

기존 코드 수정 없이 Turso 사용 가능:
  - await conn.execute(sql, params)
  - await conn.executescript(sql)
  - async with conn.execute(...) as cur: rows = await cur.fetchall()
  - await conn.commit()
  - conn.row_factory = ...  (호환용 no-op)

내부: libsql-experimental (동기) + asyncio.run_in_executor
"""

import asyncio
import logging
import os
from functools import partial
from typing import Any, Optional

logger = logging.getLogger(__name__)

# libsql-experimental 임포트
try:
    import libsql_experimental as _libsql
    _TURSO_AVAILABLE = True
except ImportError:
    _TURSO_AVAILABLE = False
    logger.warning("[TursoAdapter] libsql_experimental 미설치 → aiosqlite fallback")

_TURSO_URL   = os.getenv("TURSO_URL", "")
_TURSO_TOKEN = os.getenv("TURSO_TOKEN", "")
_LOCAL_PATH  = os.getenv("TURSO_LOCAL_PATH", "/tmp/copy_perp_replica.db")


def is_turso_configured() -> bool:
    return bool(_TURSO_URL and _TURSO_TOKEN and _TURSO_AVAILABLE)


class TursoRow(dict):
    """dict + 정수 인덱스 접근 지원 (aiosqlite.Row 호환)"""
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _make_rows(raw_rows, description) -> list:
    if not description or not raw_rows:
        return []
    cols = [d[0] for d in description]
    return [TursoRow(zip(cols, row)) for row in raw_rows]


class TursoCursor:
    """aiosqlite 커서 인터페이스 호환"""
    def __init__(self, rows: list):
        self._rows = rows

    async def fetchall(self):
        return self._rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


class TursoConnection:
    """
    libsql-experimental 동기 연결을 asyncio에서 aiosqlite처럼 사용하는 래퍼.

    사용법:
        conn = await TursoConnection.connect(url, token)
        cur = await conn.execute("SELECT ...", params)
        rows = await cur.fetchall()
        await conn.commit()
    """

    def __init__(self, raw_conn, loop):
        self._conn = raw_conn
        self._loop = loop
        self.row_factory = None  # 호환용 no-op

    @classmethod
    async def connect(cls, url: str, auth_token: str, local_path: str = ":memory:") -> "TursoConnection":
        loop = asyncio.get_event_loop()

        def _connect():
            c = _libsql.connect(local_path, sync_url=url, auth_token=auth_token)
            try:
                c.sync()
            except Exception as e:
                logger.debug(f"[Turso] 초기 sync 경고: {e}")
            return c

        raw = await loop.run_in_executor(None, _connect)
        logger.info(f"[Turso] 연결 성공: {url}")
        return cls(raw, loop)

    async def execute(self, sql: str, params=None) -> "TursoCursor":
        def _exec():
            try:
                if params is not None:
                    r = self._conn.execute(sql, params)
                else:
                    r = self._conn.execute(sql)
                desc = getattr(r, 'description', None)
                try:
                    raw_rows = r.fetchall()
                except Exception:
                    raw_rows = []
                return _make_rows(raw_rows, desc)
            except Exception as e:
                err = str(e).lower()
                if any(kw in err for kw in ("duplicate", "already exists", "no such column")):
                    logger.debug(f"[Turso] execute warning (expected): {e}")
                    return []
                raise

        rows = await self._loop.run_in_executor(None, _exec)
        return TursoCursor(rows)

    async def executemany(self, sql: str, params_list) -> None:
        def _exec():
            for p in params_list:
                self._conn.execute(sql, p)
        await self._loop.run_in_executor(None, _exec)

    async def executescript(self, script: str) -> None:
        """세미콜론으로 분리된 SQL 스크립트 실행"""
        def _exec():
            stmts = [s.strip() for s in script.split(';') if s.strip()]
            for stmt in stmts:
                try:
                    self._conn.execute(stmt)
                except Exception as e:
                    err = str(e).lower()
                    if any(kw in err for kw in ("already exists", "duplicate column", "no such")):
                        logger.debug(f"[Turso] executescript skip: {e} | stmt={stmt[:50]!r}")
                    else:
                        logger.warning(f"[Turso] executescript error: {e} | stmt={stmt[:80]!r}")
        await self._loop.run_in_executor(None, _exec)

    async def commit(self) -> None:
        def _commit():
            self._conn.commit()
            try:
                self._conn.sync()
            except Exception as e:
                logger.debug(f"[Turso] sync 경고: {e}")
        await self._loop.run_in_executor(None, _commit)

    async def rollback(self) -> None:
        pass  # libsql-experimental은 명시적 rollback 미지원

    async def close(self) -> None:
        def _close():
            try:
                self._conn.sync()
            except Exception:
                pass
            self._conn.close()
        await self._loop.run_in_executor(None, _close)

    async def sync(self) -> None:
        await self._loop.run_in_executor(None, self._conn.sync)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()


# ── 싱글턴 관리 ─────────────────────────────────────────────────────────────

_turso_conn: Optional[TursoConnection] = None


async def get_turso_connection() -> TursoConnection:
    """싱글턴 Turso 연결 반환"""
    global _turso_conn
    if _turso_conn is None:
        _turso_conn = await TursoConnection.connect(
            url=_TURSO_URL,
            auth_token=_TURSO_TOKEN,
            local_path=_LOCAL_PATH,
        )
    return _turso_conn


async def close_turso_connection() -> None:
    global _turso_conn
    if _turso_conn:
        await _turso_conn.close()
        _turso_conn = None
