"""
트레이더 통계 계산 모듈
거래 내역 기반 Win/Lose, PF, Sharpe 등 계산
"""
import logging
import math
import time
import uuid
from datetime import datetime, timezone, timedelta
from collections import defaultdict
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


async def compute_follower_pnl_report(db, follower_address: str, days: int = 30) -> dict:
    """
    팔로워 PnL 리포트 — 기간별 집계, Sharpe, MDD, by_trader/symbol, daily equity curve 포함

    Args:
        db: aiosqlite 연결
        follower_address: 팔로워 지갑 주소
        days: 조회 기간 (기본 30일)

    Returns:
        dict with total_pnl, win_rate, sharpe, max_drawdown_pct, by_trader, by_symbol,
              daily_equity, period_summary 등
    """
    from db.database import get_copy_trades

    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - days * 86400 * 1000

    all_trades = await get_copy_trades(db, follower=follower_address, limit=10000)
    # filled + 기간 필터
    trades = [
        t for t in all_trades
        if t.get("status") == "filled"
        and (t.get("created_at") or 0) > cutoff_ms
    ]

    # ── 기본 집계 ────────────────────────────────────
    wins = [t for t in trades if float(t.get("pnl", 0) or 0) > 0]
    losses = [t for t in trades if float(t.get("pnl", 0) or 0) < 0]
    total_pnl = sum(float(t.get("pnl", 0) or 0) for t in trades)
    gross_profit = sum(float(t.get("pnl", 0) or 0) for t in wins)
    gross_loss = abs(sum(float(t.get("pnl", 0) or 0) for t in losses))
    win_rate = len(wins) / len(trades) * 100 if trades else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)

    # ── ROI 추정 (팔로워 max_position_usdc 기반) ─────
    # 개별 max_position_usdc를 알 수 없으므로 10x 레버리지 가정
    # total_roi_pct = total_pnl / (avg_position * 10) * 100 — avg_position은 amount*price 평균
    try:
        positions = [
            float(t.get("amount", 0) or 0) * float(t.get("price", 0) or 1)
            for t in trades
            if float(t.get("amount", 0) or 0) > 0
        ]
        avg_pos = sum(positions) / len(positions) if positions else 100.0
        total_roi_pct = total_pnl / (avg_pos * 10) * 100 if avg_pos > 0 else 0.0
    except Exception:
        total_roi_pct = 0.0

    # ── 연속 승/패 ────────────────────────────────────
    sorted_trades = sorted(trades, key=lambda t: t.get("created_at") or 0)
    max_con_win = cur_win = 0
    max_con_lose = cur_lose = 0
    for t in sorted_trades:
        pnl = float(t.get("pnl", 0) or 0)
        if pnl > 0:
            cur_win += 1; cur_lose = 0
            max_con_win = max(max_con_win, cur_win)
        elif pnl < 0:
            cur_lose += 1; cur_win = 0
            max_con_lose = max(max_con_lose, cur_lose)

    # ── avg_hold_duration_sec ─────────────────────────
    hold_durations = [
        t.get("hold_duration_sec")
        for t in trades
        if t.get("hold_duration_sec") is not None
    ]
    avg_hold_duration_sec = sum(hold_durations) / len(hold_durations) if hold_durations else 0.0

    # ── by_trader 집계 ────────────────────────────────
    trader_map: dict = defaultdict(lambda: {"pnl": 0.0, "trade_count": 0, "win_count": 0, "trader_alias": ""})
    for t in trades:
        addr = t.get("trader_address") or "unknown"
        trader_map[addr]["pnl"] += float(t.get("pnl", 0) or 0)
        trader_map[addr]["trade_count"] += 1
        if float(t.get("pnl", 0) or 0) > 0:
            trader_map[addr]["win_count"] += 1
        trader_map[addr]["trader_alias"] = t.get("trader_alias") or ""
    by_trader = [
        {"trader_address": addr, **info}
        for addr, info in trader_map.items()
    ]
    by_trader.sort(key=lambda x: x["pnl"], reverse=True)

    # ── by_symbol 집계 ────────────────────────────────
    symbol_map: dict = defaultdict(lambda: {"pnl": 0.0, "trade_count": 0, "win_count": 0})
    for t in trades:
        sym = t.get("symbol") or "UNKNOWN"
        symbol_map[sym]["pnl"] += float(t.get("pnl", 0) or 0)
        symbol_map[sym]["trade_count"] += 1
        if float(t.get("pnl", 0) or 0) > 0:
            symbol_map[sym]["win_count"] += 1
    by_symbol = []
    for sym, info in symbol_map.items():
        tc = info["trade_count"]
        wc = info["win_count"]
        by_symbol.append({
            "symbol": sym,
            "pnl": info["pnl"],
            "trade_count": tc,
            "win_count": wc,
            "win_rate": round(wc / tc * 100, 2) if tc > 0 else 0.0,
        })
    by_symbol.sort(key=lambda x: x["pnl"], reverse=True)

    # ── daily_equity curve ────────────────────────────
    date_pnl: dict = defaultdict(float)
    date_trade_count: dict = defaultdict(int)
    for t in sorted_trades:
        ts_ms = t.get("created_at") or 0
        date_str = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        date_pnl[date_str] += float(t.get("pnl", 0) or 0)
        date_trade_count[date_str] += 1

    initial_equity = 10000.0
    equity = initial_equity
    cum_pnl = 0.0
    daily_equity = []
    for date_str in sorted(date_pnl.keys()):
        dpnl = date_pnl[date_str]
        equity += dpnl
        cum_pnl += dpnl
        cum_roi_pct = cum_pnl / initial_equity * 100
        daily_equity.append({
            "date": date_str,
            "equity": round(equity, 4),
            "daily_pnl": round(dpnl, 4),
            "cum_roi_pct": round(cum_roi_pct, 4),
        })

    # ── Sharpe (일별 PnL 기반, sqrt(252) 연율화) ──────
    daily_pnl_values = [d["daily_pnl"] for d in daily_equity]
    if len(daily_pnl_values) >= 2:
        mean_d = sum(daily_pnl_values) / len(daily_pnl_values)
        variance = sum((x - mean_d) ** 2 for x in daily_pnl_values) / len(daily_pnl_values)
        std_d = math.sqrt(variance)
        sharpe = (mean_d / std_d * math.sqrt(252)) if std_d > 0 else 0.0
    else:
        sharpe = 0.0

    # ── MDD (equity curve peak 대비 최대 낙폭) ─────────
    peak = initial_equity
    max_drawdown_pct = 0.0
    running_equity = initial_equity
    for d in daily_equity:
        running_equity = d["equity"]
        if running_equity > peak:
            peak = running_equity
        dd = (peak - running_equity) / peak * 100 if peak > 0 else 0.0
        if dd > max_drawdown_pct:
            max_drawdown_pct = dd

    # ── period_summary (1d / 7d / 30d) ───────────────
    def _period_filter(period_days: int):
        cutoff = now_ms - period_days * 86400 * 1000
        subset = [t for t in all_trades if t.get("status") == "filled" and (t.get("created_at") or 0) > cutoff]
        pnl_sum = sum(float(t.get("pnl", 0) or 0) for t in subset)
        pos = [float(t.get("amount", 0) or 0) * float(t.get("price", 0) or 1) for t in subset if float(t.get("amount", 0) or 0) > 0]
        avg_p = sum(pos) / len(pos) if pos else 100.0
        roi = pnl_sum / (avg_p * 10) * 100 if avg_p > 0 else 0.0
        return {"pnl": round(pnl_sum, 4), "roi_pct": round(roi, 4), "trade_count": len(subset)}

    period_summary = {
        "1d": _period_filter(1),
        "7d": _period_filter(7),
        "30d": _period_filter(30),
    }

    return {
        "address": follower_address,
        "period_days": days,
        "total_pnl": round(total_pnl, 4),
        "total_roi_pct": round(total_roi_pct, 4),
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 4),
        "sharpe": round(sharpe, 4),
        "max_drawdown_pct": round(max_drawdown_pct, 4),
        "consecutive_wins": max_con_win,
        "consecutive_losses": max_con_lose,
        "avg_hold_duration_sec": round(avg_hold_duration_sec, 2),
        "by_trader": by_trader,
        "by_symbol": by_symbol,
        "daily_equity": daily_equity,
        "period_summary": period_summary,
    }


async def build_daily_equity_snapshots(
    db,
    address: str,
    role: str = "follower",
    initial_equity: float = 10000.0
) -> int:
    """
    daily_equity 리스트를 performance_snapshots 테이블에 upsert

    Args:
        db: aiosqlite 연결
        address: 팔로워(또는 트레이더) 주소
        role: "follower" 또는 "trader"
        initial_equity: 초기 자산 (기본 10000 USDC)

    Returns:
        upserted 행 수
    """
    report = await compute_follower_pnl_report(db, follower_address=address, days=365)
    daily_equity = report.get("daily_equity", [])
    if not daily_equity:
        return 0

    now_ms = int(time.time() * 1000)
    upserted = 0
    cum_pnl = 0.0
    prev_equity = initial_equity

    for entry in daily_equity:
        date_str = entry["date"]
        equity = entry["equity"]
        daily_pnl = entry["daily_pnl"]
        cum_pnl += daily_pnl
        cum_roi_pct = cum_pnl / initial_equity * 100 if initial_equity > 0 else 0.0
        daily_roi_pct = daily_pnl / prev_equity * 100 if prev_equity > 0 else 0.0
        prev_equity = equity

        snap_id = str(uuid.uuid4())
        try:
            await db.execute(
                """INSERT INTO performance_snapshots
                   (id, address, role, snapshot_date, equity, daily_pnl, daily_roi_pct,
                    cum_pnl, cum_roi_pct, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(address, snapshot_date) DO UPDATE SET
                       equity = excluded.equity,
                       daily_pnl = excluded.daily_pnl,
                       daily_roi_pct = excluded.daily_roi_pct,
                       cum_pnl = excluded.cum_pnl,
                       cum_roi_pct = excluded.cum_roi_pct""",
                (snap_id, address, role, date_str, equity, daily_pnl,
                 daily_roi_pct, cum_pnl, cum_roi_pct, now_ms)
            )
            upserted += 1
        except Exception as e:
            logger.warning(f"snapshot upsert 실패 {date_str}: {e}")

    await db.commit()
    return upserted


if __name__ == "__main__":
    import sys
    addr = sys.argv[1] if len(sys.argv) > 1 else "EcX5xSDT45Nvhi2gMTjTnhF3KT2w4sPF54esEZS3hwZu"
    logger.info(f"=== {addr[:16]}... 통계 ===")
    s = get_trader_stats(addr)
    for k, v in s.items():
        if k != "address":
            logger.info(f"  {k:25}: {v}")
