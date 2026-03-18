"""
트레이더 통계 계산 모듈
거래 내역 기반 Win/Lose, PF, Sharpe 등 계산
"""
import logging
import sys
sys.path.insert(0, '.')
from dotenv import load_dotenv; load_dotenv('.env')

logger = logging.getLogger(__name__)


def compute_trader_stats(trades: list) -> dict:
    """
    거래 내역 리스트 → 통계 dict 계산
    
    Args:
        trades: [{"pnl": "123.45", "side": "open_long", ...}, ...]
    
    Returns:
        {
            "total_trades": int,
            "win_count": int, "lose_count": int,
            "win_rate": float,          # 0~100 %
            "profit_factor": float,     # gross_profit / gross_loss
            "avg_win": float,
            "avg_loss": float,
            "max_win": float,
            "max_loss": float,
            "gross_profit": float,
            "gross_loss": float,
            "net_pnl": float,
            "sharpe_proxy": float,      # net_pnl / max_dd (단순 근사)
            "max_drawdown_pct": float,  # %
            "calmar_ratio": float,      # net_pnl / max_dd
            "consecutive_wins": int,
            "consecutive_losses": int,
        }
    """
    wins = []
    losses = []
    equity = 10000.0
    peak = equity
    max_dd = 0.0
    max_con_win = cur_win = 0
    max_con_lose = cur_lose = 0

    filled_trades = [tr for tr in trades if tr.get("status") == "filled"]
    failed_trades = [tr for tr in trades if tr.get("status") in ("failed", "error")]

    for tr in filled_trades:
        pnl = float(tr.get("pnl", 0) or 0)
        if pnl == 0:
            continue
        equity += pnl
        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)

        if pnl > 0:
            wins.append(pnl)
            cur_win += 1
            cur_lose = 0
            max_con_win = max(max_con_win, cur_win)
        else:
            losses.append(abs(pnl))
            cur_lose += 1
            cur_win = 0
            max_con_lose = max(max_con_lose, cur_lose)

    total_filled = len(wins) + len(losses)
    total_all    = len(trades)
    failed_count = len(failed_trades)
    gross_profit = sum(wins)
    gross_loss   = sum(losses)
    net_pnl      = gross_profit - gross_loss
    win_rate     = len(wins) / total_filled * 100 if total_filled > 0 else 0
    success_rate = total_filled / total_all * 100 if total_all > 0 else 0
    avg_win      = sum(wins) / len(wins) if wins else 0
    avg_loss     = sum(losses) / len(losses) if losses else 0
    pf           = avg_win / avg_loss if avg_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
    sharpe       = net_pnl / max_dd if max_dd > 0 else 0
    calmar       = net_pnl / (max_dd / 100 * 10000) if max_dd > 0 else 0

    return {
        "total_trades":        total_all,
        "filled":              total_filled,
        "failed":              failed_count,
        "win_count":           len(wins),
        "loss_count":          len(losses),
        "lose_count":          len(losses),   # 하위 호환
        "win_rate":            round(win_rate, 2),
        "success_rate":        round(success_rate, 2),
        "total_pnl":           round(net_pnl, 4),
        "net_pnl":             round(net_pnl, 4),   # 하위 호환
        "profit_factor":       round(pf, 4),
        "avg_win":             round(avg_win, 4),
        "avg_loss":            round(avg_loss, 4),
        "max_win":             round(max(wins, default=0), 4),
        "max_loss":            round(max(losses, default=0), 4),
        "gross_profit":        round(gross_profit, 4),
        "gross_loss":          round(gross_loss, 4),
        "sharpe_proxy":        round(sharpe, 4),
        "max_drawdown_pct":    round(max_dd, 4),
        "calmar_ratio":        round(calmar, 4),
        "consecutive_wins":    max_con_win,
        "consecutive_losses":  max_con_lose,
    }


def get_trader_stats(address: str, limit: int = 100) -> dict:
    """Pacifica API에서 거래내역 가져와 통계 계산"""
    try:
        from pacifica.client import _cf_request
        result = _cf_request("GET", f"trades/history?account={address}&limit={limit}")
        trades = result.get("data", result) if isinstance(result, dict) else result
        if not isinstance(trades, list):
            return {"error": "no trades data", "total_trades": 0}
        stats = compute_trader_stats(trades)
        stats["address"] = address
        return stats
    except Exception as e:
        logger.error(f"stats 조회 실패 {address[:12]}: {e}")
        return {"error": str(e), "total_trades": 0}


async def get_follower_stats(db, follower_address: str) -> dict:
    """팔로워 통계 (비동기) — DB copy_trades 기반"""
    try:
        from db.database import get_copy_trades
        trades = await get_copy_trades(db, follower=follower_address, limit=500)
        filled = [t for t in trades if t.get("status") == "filled"]
        failed = [t for t in trades if t.get("status") == "failed"]
        wins = [t for t in filled if float(t.get("pnl", 0) or 0) > 0]
        losses = [t for t in filled if float(t.get("pnl", 0) or 0) < 0]
        total_pnl = sum(float(t.get("pnl", 0) or 0) for t in filled)
        total_vol = sum(float(t.get("amount", 0) or 0) * float(t.get("price", 0) or 0) for t in filled)
        win_rate = len(wins) / len(filled) if filled else 0
        return {
            "address": follower_address,
            "filled": len(filled),
            "failed": len(failed),
            "total": len(trades),
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "total_volume_usdc": total_vol,
        }
    except Exception as e:
        logger.error(f"follower stats 조회 실패 {follower_address[:12]}: {e}")
        return {
            "address": follower_address,
            "filled": 0, "failed": 0, "total": 0,
            "win_count": 0, "loss_count": 0, "win_rate": 0,
            "total_pnl": 0, "total_volume_usdc": 0,
        }


async def get_platform_stats(db) -> dict:
    """플랫폼 전체 통계 (비동기)"""
    try:
        from db.database import get_leaderboard, get_copy_trades
        import aiosqlite

        traders_raw = await get_leaderboard(db, limit=1000)

        # 팔로워 수: followers 테이블 직접 조회
        async with db.execute("SELECT COUNT(DISTINCT address) FROM followers WHERE active=1") as cur:
            row = await cur.fetchone()
            follower_count = row[0] if row else 0

        all_trades = await get_copy_trades(db, limit=100000)
        filled_trades = [t for t in all_trades if t.get("status") == "filled"]
        total_pnl = sum(float(t.get("pnl", 0) or 0) for t in filled_trades)
        total_vol = sum(float(t.get("amount", 0) or 0) * float(t.get("price", 0) or 0) for t in filled_trades)

        return {
            "active_traders": len(traders_raw),
            "active_followers": follower_count,
            "total_trades_filled": len(filled_trades),
            "total_pnl_usdc": total_pnl,
            "total_volume_usdc": total_vol,
        }
    except Exception as e:
        logger.error(f"platform stats 오류: {e}")
        return {
            "active_traders": 0,
            "active_followers": 0,
            "total_trades_filled": 0,
            "total_pnl_usdc": 0,
            "total_volume_usdc": 0,
        }


if __name__ == "__main__":
    import sys
    addr = sys.argv[1] if len(sys.argv) > 1 else "EcX5xSDT45Nvhi2gMTjTnhF3KT2w4sPF54esEZS3hwZu"
    logger.info(f"=== {addr[:16]}... 통계 ===")
    s = get_trader_stats(addr)
    for k, v in s.items():
        if k != "address":
            logger.info(f"  {k:25}: {v}")
