"""
성과 통계 계산
트레이더/팔로워 PnL, 승률, 카피 성과 집계
"""
from typing import List, Dict
from db.models import DB


class StatsEngine:
    def __init__(self, db: DB):
        self.db = db

    async def trader_stats(self, trader_id: str) -> dict:
        """트레이더 성과 요약"""
        trades = await self.db.get_copy_trades_by_trader(trader_id)
        return self._calc_stats(trader_id, trades, role="trader")

    async def follower_stats(self, follower_id: str) -> dict:
        """팔로워 성과 요약"""
        trades = await self.db.get_copy_trades_by_follower(follower_id)
        return self._calc_stats(follower_id, trades, role="follower")

    async def leaderboard(self, limit: int = 20) -> list:
        """트레이더 리더보드 (팔로워 수 기준)"""
        return await self.db.get_trader_leaderboard(limit)

    def _calc_stats(self, address: str, trades: list, role: str) -> dict:
        total = len(trades)
        filled = [t for t in trades if t["status"] == "filled"]
        failed = [t for t in trades if t["status"] in ("failed", "error")]
        skipped = [t for t in trades if t["status"] == "skipped_insufficient_balance"]

        amount_key = "follower_amount" if role == "follower" else "trader_amount"
        total_volume = sum(t.get(amount_key, 0) for t in filled)

        return {
            "address": address,
            "total_copy_trades": total,
            "filled": len(filled),
            "failed": len(failed),
            "skipped": len(skipped),
            "success_rate": round(len(filled) / total * 100, 1) if total else 0,
            "total_volume_usd": round(total_volume, 2),
        }
