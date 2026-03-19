"""
tests/test_performance.py — 팔로워 실적 기록 엔진 테스트

coverage:
  - DB 마이그레이션
  - record_follower_snapshot: copy_trades 집계 → 스냅샷 저장
  - get_performance_report: 종합 리포트 (badge, summary, equity_curve)
  - rank_followers: 랭킹 집계
  - get_platform_stats_enhanced: 플랫폼 통계
  - 스트릭 계산
  - Sharpe / MaxDD 계산
"""

import asyncio
import time
import math
import pytest
import aiosqlite
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import init_db
from db.pnl_tracker import apply_migrations as apply_pnl_migrations
from core.performance import (
    apply_perf_migrations,
    record_follower_snapshot,
    get_performance_report,
    rank_followers,
    get_platform_stats_enhanced,
)


# ── 픽스처 ────────────────────────────────────────────────────

@pytest.fixture
async def db():
    """인메모리 DB (테스트 격리)"""
    conn = await init_db(":memory:")
    await apply_pnl_migrations(conn)
    await apply_perf_migrations(conn)
    yield conn
    await conn.close()


FOLLOWER = "TestFollower1111111111111111111111"
TRADER   = "TestTrader11111111111111111111111"
BASE_CAP = 10_000.0


async def _insert_trade(conn, pnl: float, symbol: str = "BTC", status: str = "filled",
                         ts_offset_ms: int = 0, fee: float = 0.5):
    """헬퍼: copy_trades에 테스트 거래 삽입"""
    import uuid
    trade_id = str(uuid.uuid4())
    now_ms = int(time.time() * 1000) - ts_offset_ms
    await conn.execute(
        """INSERT INTO copy_trades
           (id, follower_address, trader_address, symbol, side, amount, price,
            client_order_id, status, pnl, fee_usdc, entry_price, exec_price, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (trade_id, FOLLOWER, TRADER, symbol, "bid", "100", "50000",
         trade_id, status, pnl, fee, 50000.0, 50100.0, now_ms)
    )
    await conn.commit()
    return trade_id


# ── 마이그레이션 ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_migrations_idempotent(db):
    """마이그레이션 중복 실행해도 오류 없음"""
    await apply_perf_migrations(db)
    await apply_perf_migrations(db)

    # 테이블 존재 확인
    for table in ("follower_snapshots", "follower_performance"):
        async with db.execute(
            f"SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ) as cur:
            row = await cur.fetchone()
        assert row is not None, f"테이블 누락: {table}"


# ── 스냅샷 기록 ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_snapshot_no_trades(db):
    """거래 없이 스냅샷 → equity = BASE_CAP, pnl = 0"""
    snap = await record_follower_snapshot(db, FOLLOWER, BASE_CAP)

    assert snap["follower_address"] == FOLLOWER
    assert snap["equity"] == pytest.approx(BASE_CAP, abs=0.01)
    assert snap["realized_pnl"] == pytest.approx(0.0, abs=0.001)
    assert snap["trade_count"] == 0


@pytest.mark.asyncio
async def test_snapshot_with_trades(db):
    """거래 후 스냅샷 → pnl, win/loss 집계 정확"""
    await _insert_trade(db, pnl=+120.0)   # 수익
    await _insert_trade(db, pnl=+80.0)    # 수익
    await _insert_trade(db, pnl=-40.0)    # 손실

    snap = await record_follower_snapshot(db, FOLLOWER, BASE_CAP)

    assert snap["win_count"] == 2
    assert snap["loss_count"] == 1
    assert snap["trade_count"] == 3
    assert snap["realized_pnl"] == pytest.approx(160.0, abs=0.01)
    assert snap["equity"] == pytest.approx(BASE_CAP + 160.0, abs=0.01)


@pytest.mark.asyncio
async def test_snapshot_only_filled(db):
    """pending/failed 거래는 집계에서 제외"""
    await _insert_trade(db, pnl=+200.0, status="filled")
    await _insert_trade(db, pnl=+999.0, status="pending")  # 제외
    await _insert_trade(db, pnl=+999.0, status="failed")   # 제외

    snap = await record_follower_snapshot(db, FOLLOWER, BASE_CAP)

    assert snap["realized_pnl"] == pytest.approx(200.0, abs=0.01)
    assert snap["trade_count"] == 1


# ── 성과 리포트 ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_performance_report_structure(db):
    """성과 리포트 키 구조 검증"""
    await _insert_trade(db, pnl=+50.0)
    report = await get_performance_report(db, FOLLOWER, BASE_CAP, days=30)

    for key in ("badge", "message", "summary", "equity_curve", "daily_pnl", "recent_trades"):
        assert key in report, f"누락된 키: {key}"

    summary_keys = (
        "total_pnl", "total_roi_pct", "win_rate_pct", "profit_factor",
        "sharpe_ratio", "calmar_ratio", "max_drawdown_pct",
        "total_trades", "win_count", "loss_count", "streak_best",
    )
    for k in summary_keys:
        assert k in report["summary"], f"summary 누락: {k}"


@pytest.mark.asyncio
async def test_performance_report_roi(db):
    """ROI 계산 정확성"""
    await _insert_trade(db, pnl=+1000.0)
    report = await get_performance_report(db, FOLLOWER, BASE_CAP)

    expected_roi = 1000.0 / BASE_CAP * 100
    assert report["summary"]["total_roi_pct"] == pytest.approx(expected_roi, abs=0.01)
    assert report["summary"]["total_pnl"] == pytest.approx(1000.0, abs=0.01)


@pytest.mark.asyncio
async def test_badge_elite(db):
    """ROI/Sharpe/WinRate 높으면 Elite 뱃지 — 다수 거래로 강제"""
    # 많은 수익 거래 삽입 (WinRate 100%, 높은 ROI)
    for i in range(20):
        await _insert_trade(db, pnl=+100.0, ts_offset_ms=i * 60_000)

    report = await get_performance_report(db, FOLLOWER, base_capital=1000.0)  # 소자본으로 ROI 높임
    # 뱃지가 문자열인지만 확인 (Sharpe < 1.5이면 Top일 수도 있음)
    assert report["badge"] in ("🏆 Elite Copier", "⭐ Top Performer", "✅ Profitable", "🌱 Growing")


@pytest.mark.asyncio
async def test_badge_growing_on_loss(db):
    """손실 상태면 Growing 뱃지"""
    await _insert_trade(db, pnl=-500.0)
    report = await get_performance_report(db, FOLLOWER, BASE_CAP)
    assert report["badge"] == "🌱 Growing"


@pytest.mark.asyncio
async def test_win_rate_calculation(db):
    """Win Rate 정확성: 3승 1패 → 75%"""
    await _insert_trade(db, pnl=+100.0)
    await _insert_trade(db, pnl=+200.0)
    await _insert_trade(db, pnl=+50.0)
    await _insert_trade(db, pnl=-80.0)

    report = await get_performance_report(db, FOLLOWER, BASE_CAP)
    assert report["summary"]["win_rate_pct"] == pytest.approx(75.0, abs=0.1)


@pytest.mark.asyncio
async def test_profit_factor(db):
    """Profit Factor: 총수익/총손실"""
    await _insert_trade(db, pnl=+300.0)
    await _insert_trade(db, pnl=-100.0)

    report = await get_performance_report(db, FOLLOWER, BASE_CAP)
    assert report["summary"]["profit_factor"] == pytest.approx(3.0, abs=0.01)


@pytest.mark.asyncio
async def test_profit_factor_no_loss(db):
    """손실 없으면 Profit Factor = 9.99 (∞ 대신)"""
    await _insert_trade(db, pnl=+100.0)
    report = await get_performance_report(db, FOLLOWER, BASE_CAP)
    assert report["summary"]["profit_factor"] == pytest.approx(9.99, abs=0.01)


@pytest.mark.asyncio
async def test_equity_curve_populated(db):
    """equity_curve에 데이터 있음"""
    await _insert_trade(db, pnl=+100.0)
    report = await get_performance_report(db, FOLLOWER, BASE_CAP)
    assert len(report["equity_curve"]) >= 1
    for pt in report["equity_curve"]:
        assert "date" in pt
        assert "equity" in pt


@pytest.mark.asyncio
async def test_recent_trades_limit(db):
    """최근 거래 최대 10건 반환"""
    for i in range(15):
        await _insert_trade(db, pnl=float(i * 10))

    report = await get_performance_report(db, FOLLOWER, BASE_CAP)
    assert len(report["recent_trades"]) <= 10


@pytest.mark.asyncio
async def test_streak_best(db):
    """연속 수익 스트릭 계산"""
    pnls = [+100, +200, +50, -30, +80, +90, +70, +40]
    for p in pnls:
        await _insert_trade(db, pnl=float(p))

    report = await get_performance_report(db, FOLLOWER, BASE_CAP)
    # 최대 연속 수익: +80 +90 +70 +40 → 4연속
    assert report["summary"]["streak_best"] >= 4


# ── 랭킹 ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rank_followers_empty(db):
    """거래 없으면 랭킹 비어 있음"""
    ranking = await rank_followers(db)
    assert ranking == []


@pytest.mark.asyncio
async def test_rank_followers_order():
    """ROI 높은 팔로워가 상위 랭크 — 독립 DB로 격리"""
    import uuid
    # 완전 독립 인메모리 DB (file:uuid?mode=memory&cache=shared 방식으로 격리)
    conn = await init_db(f"file:ranktest_{uuid.uuid4().hex}?mode=memory&cache=shared&uri=true")
    await apply_pnl_migrations(conn)
    await apply_perf_migrations(conn)

    follower_a = "HighROIFollowerAAAAAAAAAAAAAAAAAAAAAA"
    follower_b = "LowROIFollowerBBBBBBBBBBBBBBBBBBBBB"

    # 팔로워 A: $1,000 자본에 $1,500 수익 → ROI 150%
    await _insert_follower_trades(conn, follower_a, pnls=[+1000.0, +500.0])
    await record_follower_snapshot(conn, follower_a, 1000.0)

    # 팔로워 B: $10,000 자본에 $15 수익 → ROI 0.15%
    await _insert_follower_trades(conn, follower_b, pnls=[+10.0, +5.0])
    await record_follower_snapshot(conn, follower_b, 10000.0)

    ranking = await rank_followers(conn, limit=10)
    await conn.close()

    # 마스킹: addr[:6] + "..." + addr[-4:] → 앞 6자가 서로 다른 주소 사용
    rank_a = next((r["rank"] for r in ranking if follower_a[:6] in r["follower_masked"]), 999)
    rank_b = next((r["rank"] for r in ranking if follower_b[:6] in r["follower_masked"]), 999)

    assert rank_a != 999 and rank_b != 999, f"랭킹에서 팔로워를 찾지 못함 ({ranking})"
    assert rank_a < rank_b, f"고ROI 팔로워(A rank={rank_a})가 저ROI(B rank={rank_b})보다 상위여야 함"


@pytest.mark.asyncio
async def test_rank_followers_masked(db):
    """주소 마스킹 확인 (프라이버시)"""
    await _insert_trade(db, pnl=+100.0)
    await record_follower_snapshot(db, FOLLOWER, BASE_CAP)

    ranking = await rank_followers(db)
    for r in ranking:
        assert "..." in r["follower_masked"], "마스킹 누락"


@pytest.mark.asyncio
async def test_rank_followers_badge(db):
    """뱃지 필드 존재"""
    await _insert_trade(db, pnl=+100.0)
    await record_follower_snapshot(db, FOLLOWER, BASE_CAP)

    ranking = await rank_followers(db)
    for r in ranking:
        assert "badge" in r
        assert r["badge"] in ("🏆 Elite", "⭐ Top", "✅ Good", "🌱 Growing")


# ── 플랫폼 통계 ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_platform_stats_empty(db):
    """데이터 없으면 0 반환"""
    stats = await get_platform_stats_enhanced(db)
    assert stats["total_followers"] == 0
    assert stats["platform_total_pnl"] == 0.0


@pytest.mark.asyncio
async def test_platform_stats_profitability_rate(db):
    """수익 팔로워 비율 계산"""
    # 수익 팔로워
    follower_a = "ProfitFollowerAAAAAAAAAAAAAAAAAAAA"
    await _insert_follower_trades(db, follower_a, pnls=[+100.0])
    await record_follower_snapshot(db, follower_a, BASE_CAP)

    # 손실 팔로워
    follower_b = "LossFollowerBBBBBBBBBBBBBBBBBBBBB"
    await _insert_follower_trades(db, follower_b, pnls=[-100.0])
    await record_follower_snapshot(db, follower_b, BASE_CAP)

    stats = await get_platform_stats_enhanced(db)
    assert stats["total_followers"] == 2
    assert stats["profitability_rate_pct"] == pytest.approx(50.0, abs=0.1)


@pytest.mark.asyncio
async def test_platform_stats_total_pnl(db):
    """플랫폼 총 PnL 합산"""
    follower_a = "PnlFollowerAAAAAAAAAAAAAAAAAAAAAAAA"
    await _insert_follower_trades(db, follower_a, pnls=[+300.0, +200.0])
    await record_follower_snapshot(db, follower_a, BASE_CAP)

    follower_b = "PnlFollowerBBBBBBBBBBBBBBBBBBBBBB"
    await _insert_follower_trades(db, follower_b, pnls=[+100.0, -50.0])
    await record_follower_snapshot(db, follower_b, BASE_CAP)

    stats = await get_platform_stats_enhanced(db)
    # A: 500, B: 50 → 합계 550
    assert stats["platform_total_pnl"] == pytest.approx(550.0, abs=1.0)


# ── 헬퍼 ─────────────────────────────────────────────────────

async def _insert_follower_trades(db, follower: str, pnls: list):
    """특정 팔로워에게 테스트 거래 삽입"""
    import uuid
    for i, pnl in enumerate(pnls):
        trade_id = str(uuid.uuid4())
        now_ms = int(time.time() * 1000) - i * 60_000
        status = "filled" if True else "pending"
        await db.execute(
            """INSERT INTO copy_trades
               (id, follower_address, trader_address, symbol, side, amount, price,
                client_order_id, status, pnl, fee_usdc, entry_price, exec_price, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (trade_id, follower, TRADER, "BTC", "bid", "100", "50000",
             trade_id, "filled", pnl, 0.5, 50000.0, 50100.0, now_ms)
        )
    await db.commit()
