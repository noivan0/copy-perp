"""
core/db.py — DB 추상화 레이어 (Turso / aiosqlite 이중 지원)

TURSO_URL 환경변수가 설정되면 libsql_client(HTTP transport)로 Turso 연결.
설정이 없으면 aiosqlite(로컬 SQLite) fallback.

외부 코드는 이 모듈만 임포트하면 됩니다:

    from core.db import get_db, rows_to_dicts, row_to_dict

    async def foo():
        db = await get_db()
        rows = await db.fetchall("SELECT * FROM traders WHERE active=1")
        dicts = rows_to_dicts(rows)

또는 async context manager:

    async with get_db_ctx() as db:
        row = await db.fetchone("SELECT * FROM traders WHERE address=?", [addr])
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, List, Optional, Sequence, Union

logger = logging.getLogger(__name__)

# ── 환경변수 ─────────────────────────────────────────────────────────────────
_DB_PATH = os.getenv("DB_PATH", "copy_perp.db")


def is_turso_mode() -> bool:
    """런타임에 동적으로 환경변수 확인 (import 시점 캐싱 방지)"""
    return bool(os.getenv("TURSO_URL") and os.getenv("TURSO_TOKEN"))


# ── 파라미터 정규화 ──────────────────────────────────────────────────────────

def _normalize_params(params) -> Optional[list]:
    """
    aiosqlite tuple (val,) / list [val] / None → libsql_client InArgs(list)
    libsql_client는 list 또는 dict 파라미터를 받음.
    None 반환 시 파라미터 없음으로 처리.
    """
    if params is None:
        return None
    if isinstance(params, (list, tuple)):
        return list(params)
    if isinstance(params, dict):
        return params
    return list(params)


# ── libsql_client Row → dict 변환 ────────────────────────────────────────────

class DbRow(dict):
    """
    dict 기반 Row — aiosqlite.Row처럼 이름/인덱스 모두 접근 가능.

    row["column_name"]  → 이름 접근
    row[0]              → 인덱스 접근
    dict(row)           → dict 변환
    row.get("key")      → dict.get()
    """
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _result_set_to_dicts(rs) -> List[DbRow]:
    """libsql_client.ResultSet → List[DbRow]"""
    if rs is None:
        return []
    cols = list(rs.columns)
    rows = []
    for raw_row in rs.rows:
        # libsql_client.Row는 인덱스 접근 지원
        row_dict = DbRow()
        for i, col in enumerate(cols):
            val = raw_row[i]
            # libsql_client.Value: None, str, int, float, bytes — 그대로 사용
            row_dict[col] = val
        rows.append(row_dict)
    return rows


def rows_to_dicts(rows) -> List[dict]:
    """fetchall() 결과 → list[dict] (호환 헬퍼)"""
    if not rows:
        return []
    result = []
    for r in rows:
        if isinstance(r, dict):
            result.append(r)
        else:
            # aiosqlite.Row
            try:
                result.append(dict(r))
            except Exception:
                result.append({})
    return result


def row_to_dict(row) -> Optional[dict]:
    """fetchone() 결과 → dict (None 안전)"""
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    try:
        return dict(row)
    except Exception:
        return None


# ── Turso DB 커넥션 래퍼 ─────────────────────────────────────────────────────

class TursoDb:
    """
    libsql_client.Client 래퍼 — aiosqlite 패턴 호환.

    사용법 (기존 aiosqlite 코드와 동일):
        cur = await db.execute("SELECT * FROM foo WHERE id=?", [id])
        rows = await cur.fetchall()
        await db.commit()

    또는 헬퍼 메서드:
        rows = await db.fetchall("SELECT * FROM foo")
        row  = await db.fetchone("SELECT * FROM foo WHERE id=?", [id])
    """

    def __init__(self, client):
        self._client = client
        self._pending_writes: List[Any] = []  # batch 트랜잭션용
        self.row_factory = None  # 호환용 no-op
        self.rowcount: int = 0
        self.lastrowid: Optional[int] = None

    def execute(self, sql: str, params=None) -> "_ExecuteAwaitable":
        """
        aiosqlite.execute() 호환.

        두 가지 패턴 모두 지원:
          cur = await db.execute(sql)         → _TursoCursor 반환
          async with db.execute(sql) as cur:  → _TursoCursor 반환 (context manager)
        """
        return _ExecuteAwaitable(self._execute_inner(sql, params))

    async def _execute_inner(self, sql: str, params=None) -> "_TursoCursor":
        """실제 Turso 실행 coroutine"""
        norm = _normalize_params(params)
        try:
            if norm is not None:
                rs = await self._client.execute(sql, norm)
            else:
                rs = await self._client.execute(sql)
            rows = _result_set_to_dicts(rs)
            # rs is not None 으로 체크: rs가 빈 ResultSet이어도 bool(rs)==False 이므로 is None 비교 필수
            cols = list(rs.columns) if rs is not None else []
            self.rowcount = rs.rows_affected if rs is not None else 0
            self.lastrowid = rs.last_insert_rowid if rs is not None else None
        except Exception as e:
            logger.error(f"[TursoDb] execute error: {e} | sql={sql[:100]!r}")
            raise
        return _TursoCursor(rows, rowcount=self.rowcount, columns=cols)

    async def executemany(self, sql: str, params_list) -> None:
        """libsql_client에는 executemany 없음 → loop로 대체"""
        for params in params_list:
            await self.execute(sql, params)

    async def executescript(self, script: str) -> None:
        """세미콜론으로 분리된 SQL 스크립트 일괄 실행"""
        stmts = [s.strip() for s in script.split(";") if s.strip()]
        for stmt in stmts:
            try:
                await self._client.execute(stmt)
            except Exception as e:
                err = str(e).lower()
                if any(kw in err for kw in ("already exists", "duplicate column", "no such")):
                    logger.debug(f"[TursoDb] executescript skip: {e}")
                else:
                    logger.warning(f"[TursoDb] executescript error: {e} | stmt={stmt[:80]!r}")

    async def commit(self) -> None:
        """
        libsql_client HTTP transport는 자동 커밋.
        batch()로 묶인 pending 쓰기가 있으면 함께 실행.
        """
        if self._pending_writes:
            try:
                await self._client.batch(self._pending_writes)
            except Exception as e:
                logger.error(f"[TursoDb] batch commit error: {e}")
                raise
            finally:
                self._pending_writes.clear()

    async def rollback(self) -> None:
        """libsql_client HTTP transport는 명시적 rollback 미지원 — pending 초기화"""
        self._pending_writes.clear()

    async def close(self) -> None:
        try:
            await self._client.close()
        except Exception as e:
            logger.debug(f"[TursoDb] close warning: {e}")

    async def fetchall(self, sql: str, params=None) -> List[DbRow]:
        """헬퍼: execute + fetchall 한 번에"""
        cur = await self._execute_inner(sql, params)
        return await cur.fetchall()

    async def fetchone(self, sql: str, params=None) -> Optional[DbRow]:
        """헬퍼: execute + fetchone 한 번에"""
        cur = await self._execute_inner(sql, params)
        return await cur.fetchone()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()


class _TursoCursor:
    """aiosqlite 커서 인터페이스 호환"""

    def __init__(self, rows: List[DbRow], rowcount: int = 0, columns: List[str] = None):
        self._rows = rows
        self.rowcount = rowcount
        # aiosqlite 호환 description: [(col_name, None, None, None, None, None, None), ...]
        _cols = columns or (list(rows[0].keys()) if rows else [])
        self.description = [(c, None, None, None, None, None, None) for c in _cols]

    async def fetchall(self) -> List[DbRow]:
        return self._rows

    async def fetchone(self) -> Optional[DbRow]:
        return self._rows[0] if self._rows else None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


class _ExecuteAwaitable:
    """
    TursoDb.execute()의 반환값.

    두 가지 패턴 모두 지원:
      1. cur = await db.execute(sql)         → await 시 _TursoCursor 반환
      2. async with db.execute(sql) as cur:  → context manager 진입 시 _TursoCursor 반환

    aiosqlite는 execute()가 async context manager를 반환하는 반면,
    TursoDb는 execute()가 coroutine이므로 이 래퍼 클래스가 필요.
    """

    def __init__(self, coro):
        self._coro = coro
        self._cursor: Optional[_TursoCursor] = None

    def __await__(self):
        return self._coro.__await__()

    async def __aenter__(self) -> "_TursoCursor":
        self._cursor = await self._coro
        return self._cursor

    async def __aexit__(self, *_):
        pass


# ── 싱글턴 커넥션 관리 ────────────────────────────────────────────────────────

_turso_db: Optional[TursoDb] = None
_aiosqlite_db = None


async def _get_turso_db() -> TursoDb:
    """Turso 싱글턴 연결 반환 (libsql_client HTTP transport)"""
    global _turso_db
    if _turso_db is None:
        import libsql_client
        _raw_url = os.getenv("TURSO_URL", "")
        # libsql_client는 https:// 형식 필요 (libsql:// → https:// 자동 변환)
        _http_url = _raw_url.replace("libsql://", "https://") if _raw_url.startswith("libsql://") else _raw_url
        client = libsql_client.create_client(
            url=_http_url,
            auth_token=os.getenv("TURSO_TOKEN", ""),
        )
        _turso_db = TursoDb(client)
        _turso_url_log = os.getenv("TURSO_URL","")
        logger.info(f"[DB] Turso 연결 완료 (HTTP): {_turso_url_log[:60]}")
    return _turso_db


async def _get_aiosqlite_db():
    """aiosqlite 싱글턴 연결 반환 (로컬 개발용)"""
    global _aiosqlite_db
    if _aiosqlite_db is None or getattr(_aiosqlite_db, "_connection", True) is None:
        import aiosqlite
        # DB 디렉토리 자동 생성
        db_dir = os.path.dirname(os.path.abspath(_DB_PATH))
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        _aiosqlite_db = await aiosqlite.connect(_DB_PATH)
        _aiosqlite_db.row_factory = aiosqlite.Row
        logger.info(f"[DB] aiosqlite 연결: {_DB_PATH}")
    return _aiosqlite_db


async def get_db():
    """
    DB 연결 반환.

    - TURSO_URL + TURSO_TOKEN 설정 시 → TursoDb (libsql_client HTTP)
    - 미설정 시 → aiosqlite (로컬 SQLite)

    aiosqlite와 동일한 API:
        cur = await db.execute(sql, params)
        rows = await cur.fetchall()
        await db.commit()
    """
    if is_turso_mode():
        return await _get_turso_db()
    return await _get_aiosqlite_db()


@asynccontextmanager
async def get_db_ctx():
    """async context manager — 예외 시 rollback, 정상 시 commit (옵션)"""
    db = await get_db()
    try:
        yield db
    except Exception:
        await db.rollback()
        raise


async def close_db() -> None:
    """서버 종료 시 DB 연결 닫기"""
    global _turso_db, _aiosqlite_db
    if _turso_db is not None:
        await _turso_db.close()
        _turso_db = None
    if _aiosqlite_db is not None:
        await _aiosqlite_db.close()
        _aiosqlite_db = None
    logger.info("[DB] 연결 종료")


# ── requirements.txt 참고: libsql-client 패키지 필요 ─────────────────────────
# pip install libsql-client
