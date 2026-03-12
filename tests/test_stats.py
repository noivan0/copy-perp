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
