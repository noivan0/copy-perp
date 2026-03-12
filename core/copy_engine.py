"""
Copy Engine — 트레이더 포지션 변화 → 팔로워 자동 복사
"""
import asyncio
import logging
import uuid
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class PositionChange:
    type: str       # "open" / "reduce" / "close"
    symbol: str
    side: str       # "bid" / "ask"
    size_delta: float
    trader_id: str


class CopyEngine:
    """
    트레이더 포지션 변화 감지 → 팔로워 자동 복사
    """

    def __init__(self, db, pacifica_client_factory):
        self.db = db
        self.make_client = pacifica_client_factory  # fn(private_key) -> PacificaClient

    async def on_position_change(self, change: dict, trader_id: str):
        """포지션 변화 이벤트 수신"""
        log.info(f"[CHANGE] trader={trader_id} type={change['type']} {change['side']} {change['symbol']} delta={change['size_delta']}")

        followers = await self.db.get_active_followers(trader_id)
        if not followers:
            return

        tasks = [self._copy_for_follower(change, trader_id, f) for f in followers]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        ok = sum(1 for r in results if not isinstance(r, Exception))
        fail = len(results) - ok
        log.info(f"[COPY DONE] trader={trader_id} followers={len(followers)} ok={ok} fail={fail}")

    async def _copy_for_follower(self, change: dict, trader_id: str, follower: dict):
        follower_id = follower["id"]
        copy_ratio = float(follower.get("copy_ratio", 1.0))
        max_usd = float(follower.get("max_position_usd", 100))
        private_key = follower.get("private_key")

        if not private_key:
            log.warning(f"팔로워 {follower_id} private_key 없음 — 스킵")
            return

        try:
            client = self.make_client(private_key)
            balance = client.get_balance()

            if balance < 1.0:
                log.warning(f"팔로워 {follower_id} 잔고 부족 (${balance:.2f}) — 스킵")
                await self.db.save_copy_trade({
                    "id": str(uuid.uuid4()),
                    "trader_id": trader_id,
                    "follower_id": follower_id,
                    "symbol": change["symbol"],
                    "side": change["side"],
                    "trader_amount": change["size_delta"],
                    "follower_amount": 0,
                    "status": "skipped_insufficient_balance",
                })
                return

            copy_amount = min(balance * copy_ratio, max_usd)
            copy_amount = round(copy_amount, 2)

            # close 타입이면 reduce_only=True
            reduce_only = change["type"] == "close"

            order = client.market_order(
                symbol=change["symbol"],
                side=change["side"],
                amount=str(copy_amount),
                reduce_only=reduce_only,
            )

            order_id = order.get("order_id") or order.get("data", {}).get("order_id")
            status = "filled" if order_id else "failed"

            log.info(f"[COPY] follower={follower_id} order={order_id} ${copy_amount} status={status}")

            await self.db.save_copy_trade({
                "id": str(uuid.uuid4()),
                "trader_id": trader_id,
                "follower_id": follower_id,
                "symbol": change["symbol"],
                "side": change["side"],
                "trader_amount": change["size_delta"],
                "follower_amount": copy_amount,
                "status": status,
                "order_id": order_id,
            })

        except Exception as e:
            log.error(f"[COPY FAIL] follower={follower_id}: {e}")
            await self.db.save_copy_trade({
                "id": str(uuid.uuid4()),
                "trader_id": trader_id,
                "follower_id": follower_id,
                "symbol": change["symbol"],
                "side": change["side"],
                "trader_amount": change["size_delta"],
                "follower_amount": 0,
                "status": "error",
            })
