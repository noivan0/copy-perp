"""
성과 통계 계산 — aiosqlite 기반 (db.database 사용)
"""
from typing import Optional


def compute_trader_stats(trades: list) -> dict:
    """체결 내역 리스트로 통계 계산 (동기)"""
    total = len(trades)
    filled = [t for t in trades if t.get("status") == "filled"]
    failed = [t for t in trades if t.get("status") in ("failed", "error")]
    pnl_list = [t.get("pnl") or 0 for t in filled]
    volume = sum(float(t.get("amount", 0)) for t in filled)
    total_pnl = sum(pnl_list)

    wins = [p for p in pnl_list if p > 0]
    losses = [p for p in pnl_list if p < 0]

    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0
    profit_factor = avg_win / avg_loss if avg_loss else float("inf")

    return {
        "total_trades": total,
        "filled": len(filled),
        "failed": len(failed),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": round(len(wins) / len(filled) * 100, 1) if filled else 0,
        "success_rate": round(len(filled) / total * 100, 1) if total else 0,
        "total_pnl": round(total_pnl, 4),
        "volume_usd": round(volume, 4),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else None,
    }


async def get_trader_stats(db, trader_address: str) -> dict:
    """DB에서 트레이더 통계 계산"""
    async with db.execute(
        "SELECT * FROM copy_trades WHERE trader_address = ?", (trader_address,)
    ) as cur:
        trades = [dict(r) for r in await cur.fetchall()]
    stats = compute_trader_stats(trades)
    stats["trader_address"] = trader_address
    return stats


async def get_follower_stats(db, follower_address: str) -> dict:
    """DB에서 팔로워 통계 계산"""
    async with db.execute(
        "SELECT * FROM copy_trades WHERE follower_address = ?", (follower_address,)
    ) as cur:
        trades = [dict(r) for r in await cur.fetchall()]
    stats = compute_trader_stats(trades)
    stats["follower_address"] = follower_address
    return stats


async def get_platform_stats(db) -> dict:
    """플랫폼 전체 통계"""
    async with db.execute("SELECT COUNT(*) as c FROM traders WHERE active=1") as cur:
        traders = (await cur.fetchone())["c"]
    async with db.execute("SELECT COUNT(*) as c FROM followers WHERE active=1") as cur:
        followers = (await cur.fetchone())["c"]
    async with db.execute(
        "SELECT COUNT(*) as c, SUM(pnl) as pnl, SUM(CAST(amount AS REAL)) as vol FROM copy_trades WHERE status='filled'"
    ) as cur:
        row = await cur.fetchone()

    return {
        "active_traders": traders,
        "active_followers": followers,
        "total_trades_filled": row["c"] or 0,
        "total_pnl_usdc": round(row["pnl"] or 0, 4),
        "total_volume_usdc": round(row["vol"] or 0, 4),
    }
