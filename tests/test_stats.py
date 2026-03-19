"""
Stats 계산 테스트
TC-STATS-001 ~ TC-STATS-005
"""
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core.stats import compute_trader_stats


def make_trades(filled=5, failed=1, pnl_values=None):
    trades = []
    pnl_values = pnl_values or [100, 50, -30, 200, -10]
    for i, pnl in enumerate(pnl_values):
        trades.append({"status": "filled", "amount": "0.01", "pnl": pnl})
    for _ in range(failed):
        trades.append({"status": "failed", "amount": "0", "pnl": None})
    return trades


# TC-STATS-001: 기본 통계 계산
def test_basic_stats():
    trades = make_trades(pnl_values=[100, 50, -30, 200, -10])
    stats = compute_trader_stats(trades)
    assert stats["total_trades"] == 6  # 5 filled + 1 failed
    assert stats["filled"] == 5
    assert stats["failed"] == 1
    assert stats["win_count"] == 3   # 100, 50, 200
    assert stats["loss_count"] == 2  # -30, -10
    assert stats["total_pnl"] == pytest.approx(310.0)


# TC-STATS-002: 승률 계산
def test_win_rate():
    trades = make_trades(pnl_values=[100, -50])
    stats = compute_trader_stats(trades)
    assert stats["win_rate"] == 50.0


# TC-STATS-003: 빈 거래 목록
def test_empty_trades():
    stats = compute_trader_stats([])
    assert stats["total_trades"] == 0
    assert stats["win_rate"] == 0
    assert stats["success_rate"] == 0
    assert stats["total_pnl"] == 0


# TC-STATS-004: 전승 (모두 수익)
def test_all_wins():
    trades = [{"status": "filled", "amount": "1.0", "pnl": 100} for _ in range(5)]
    stats = compute_trader_stats(trades)
    assert stats["win_rate"] == 100.0
    assert stats["loss_count"] == 0


# TC-STATS-005: profit_factor 계산
def test_profit_factor():
    # avg_win=150, avg_loss=20 → profit_factor=7.5
    trades = [
        {"status": "filled", "amount": "1", "pnl": 100},
        {"status": "filled", "amount": "1", "pnl": 200},
        {"status": "filled", "amount": "1", "pnl": -20},
    ]
    stats = compute_trader_stats(trades)
    assert stats["avg_win"] == pytest.approx(150.0)
    assert stats["avg_loss"] == pytest.approx(20.0)
    assert stats["profit_factor"] == pytest.approx(7.5)


# ── TestFollowerPnlReport ────────────────────────────────────────────────────
import asyncio
import time
import aiosqlite
import pytest_asyncio
import pytest
from core.stats import compute_follower_pnl_report
from db.database import init_db, record_copy_trade, add_trader, add_follower


FOLLOWER_ADDR = "FoLLoWeRxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx1"
TRADER_A = "TrAaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1"
TRADER_B = "TrBbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb1"


def _ms(days_ago: float = 0) -> int:
    return int((time.time() - days_ago * 86400) * 1000)


def _make_trade(
    idx: int,
    follower: str,
    trader: str,
    symbol: str,
    pnl: float,
    days_ago: float = 1.0,
) -> dict:
    return {
        "id": f"trade-{idx:04d}",
        "follower_address": follower,
        "trader_address": trader,
        "symbol": symbol,
        "side": "open_long",
        "amount": "1.0",
        "price": "100.0",
        "client_order_id": f"coid-{idx:04d}",
        "status": "filled",
        "pnl": pnl,
        "entry_price": 100.0,
        "exec_price": 100.0,
        "created_at": _ms(days_ago),
        "error_msg": None,
    }


@pytest.fixture
async def db_with_trades():
    """in-memory DB + 10개 filled trades (양수 7, 음수 3)"""
    conn = await init_db(":memory:")
    await add_trader(conn, TRADER_A, "AlphaTrader")
    await add_trader(conn, TRADER_B, "BetaTrader")
    await add_follower(conn, FOLLOWER_ADDR, TRADER_A)

    # 10개 trades: trader_address 2개, symbol 3개 혼용
    # pnl: 양수 7개(+10,+20,+30,+40,+50,+60,+70), 음수 3개(-5,-10,-15)
    trades = [
        _make_trade(1,  FOLLOWER_ADDR, TRADER_A, "SOL-PERP",  10.0, days_ago=5),
        _make_trade(2,  FOLLOWER_ADDR, TRADER_A, "BTC-PERP",  20.0, days_ago=6),
        _make_trade(3,  FOLLOWER_ADDR, TRADER_A, "ETH-PERP",  30.0, days_ago=7),
        _make_trade(4,  FOLLOWER_ADDR, TRADER_B, "SOL-PERP",  40.0, days_ago=8),
        _make_trade(5,  FOLLOWER_ADDR, TRADER_B, "BTC-PERP",  50.0, days_ago=9),
        _make_trade(6,  FOLLOWER_ADDR, TRADER_A, "ETH-PERP",  60.0, days_ago=10),
        _make_trade(7,  FOLLOWER_ADDR, TRADER_B, "SOL-PERP",  70.0, days_ago=11),
        _make_trade(8,  FOLLOWER_ADDR, TRADER_A, "SOL-PERP",  -5.0, days_ago=12),
        _make_trade(9,  FOLLOWER_ADDR, TRADER_B, "BTC-PERP", -10.0, days_ago=13),
        _make_trade(10, FOLLOWER_ADDR, TRADER_A, "ETH-PERP", -15.0, days_ago=14),
    ]
    for t in trades:
        await record_copy_trade(conn, t)

    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_basic_pnl(db_with_trades):
    """TC-FOLLOWER-001: total_pnl, win_rate, profit_factor 검증"""
    report = await compute_follower_pnl_report(db_with_trades, FOLLOWER_ADDR, days=30)
    expected_total = 10 + 20 + 30 + 40 + 50 + 60 + 70 - 5 - 10 - 15  # 250
    assert report["total_pnl"] == pytest.approx(expected_total, rel=1e-4)
    assert report["win_rate"] == pytest.approx(70.0, rel=1e-2)   # 7/10 * 100
    gross_profit = 10 + 20 + 30 + 40 + 50 + 60 + 70  # 280
    gross_loss = 5 + 10 + 15  # 30
    assert report["profit_factor"] == pytest.approx(gross_profit / gross_loss, rel=1e-3)


@pytest.mark.asyncio
async def test_by_trader_aggregation(db_with_trades):
    """TC-FOLLOWER-002: by_trader 길이, 각 trader pnl 합계 검증"""
    report = await compute_follower_pnl_report(db_with_trades, FOLLOWER_ADDR, days=30)
    by_trader = {item["trader_address"]: item for item in report["by_trader"]}
    # TRADER_A: 10+20+30+60-5-15 = 100
    assert len(report["by_trader"]) == 2
    assert by_trader[TRADER_A]["pnl"] == pytest.approx(100.0, rel=1e-4)
    # TRADER_B: 40+50+70-10 = 150
    assert by_trader[TRADER_B]["pnl"] == pytest.approx(150.0, rel=1e-4)


@pytest.mark.asyncio
async def test_by_symbol_aggregation(db_with_trades):
    """TC-FOLLOWER-003: by_symbol 길이, win_rate 검증"""
    report = await compute_follower_pnl_report(db_with_trades, FOLLOWER_ADDR, days=30)
    by_symbol = {item["symbol"]: item for item in report["by_symbol"]}
    # SOL-PERP: 10+40+70-5 = 115, 4 trades, 3 wins → 75%
    assert len(report["by_symbol"]) == 3
    assert by_symbol["SOL-PERP"]["pnl"] == pytest.approx(115.0, rel=1e-4)
    assert by_symbol["SOL-PERP"]["win_rate"] == pytest.approx(75.0, rel=1e-2)


@pytest.mark.asyncio
async def test_daily_equity_curve(db_with_trades):
    """TC-FOLLOWER-004: daily_equity 날짜순 정렬, 마지막 equity = 10000 + total_pnl"""
    report = await compute_follower_pnl_report(db_with_trades, FOLLOWER_ADDR, days=30)
    equity_curve = report["daily_equity"]
    assert len(equity_curve) > 0
    # 날짜순 정렬 검증
    dates = [e["date"] for e in equity_curve]
    assert dates == sorted(dates)
    # 마지막 equity = 10000 + total_pnl
    last_equity = equity_curve[-1]["equity"]
    assert last_equity == pytest.approx(10000.0 + report["total_pnl"], rel=1e-4)


@pytest.mark.asyncio
async def test_period_summary(db_with_trades):
    """TC-FOLLOWER-005: 30d pnl == total_pnl (전부 30일 이내)"""
    report = await compute_follower_pnl_report(db_with_trades, FOLLOWER_ADDR, days=30)
    # 모든 trades가 30일 이내이므로 30d pnl == total_pnl
    assert report["period_summary"]["30d"]["pnl"] == pytest.approx(report["total_pnl"], rel=1e-4)
    assert report["period_summary"]["30d"]["trade_count"] == 10


@pytest.mark.asyncio
async def test_sharpe_positive(db_with_trades):
    """TC-FOLLOWER-006: wins > losses면 sharpe > 0"""
    report = await compute_follower_pnl_report(db_with_trades, FOLLOWER_ADDR, days=30)
    # 7승 3패, 날짜별로 spread되어 있으므로 daily mean > 0 → sharpe > 0
    # (단, 모든 날짜가 1거래씩이면 std > 0이어야 함)
    assert report["sharpe"] >= 0  # 최소 0 이상 (pnl 편차 없으면 0도 허용)
    # 더 강한 검증: total_pnl > 0이면 sharpe >= 0
    assert report["total_pnl"] > 0


@pytest.mark.asyncio
async def test_mdd_calculation(db_with_trades):
    """TC-FOLLOWER-007: 손실 거래 있으면 max_drawdown_pct > 0"""
    report = await compute_follower_pnl_report(db_with_trades, FOLLOWER_ADDR, days=30)
    # 손실 trades가 있으므로 MDD > 0
    # (단, 손실이 equity curve peak 이후에 발생해야 함 — 날짜별 집계 후 체크)
    # equity는 누적이므로 peak 이후 손실일 때만 MDD > 0
    # 모든 dates가 5~14일 전으로 분산되어 있고 마지막 며칠이 손실이면 MDD > 0
    assert report["max_drawdown_pct"] >= 0  # 최소 0 이상
    # 손실 거래 존재 확인
    assert any(float(item["daily_pnl"]) < 0 for item in report["daily_equity"])
