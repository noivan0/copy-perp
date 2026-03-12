"""
Copy Engine — 트레이더 체결 이벤트 → 팔로워 자동 복사
"""
import asyncio
import logging
import uuid
from dataclasses import dataclass

from db.models import get_active_followers, save_copy_trade
from pacifica.client import PacificaClient

log = logging.getLogger(__name__)

BUILDER_CODE = ""  # 환경변수로 주입


@dataclass
class FillEvent:
    """트레이더 체결 이벤트"""
    account: str      # 트레이더 주소
    symbol: str       # BTC, ETH, SOL ...
    side: str         # bid(롱) / ask(숏)
    amount: str       # 체결 수량 (USD)
    order_id: str
    price: float


class CopyEngine:
    """
    핵심 카피 로직
    - 트레이더 체결 이벤트 수신
    - 팔로워별 비율 계산
    - Pacifica API로 자동 복사 주문 실행
    """

    def __init__(self):
        self._running = False

    async def on_fill(self, event: FillEvent):
        """트레이더 체결 이벤트 처리"""
        log.info(f"[FILL] trader={event.account} {event.side} {event.symbol} ${event.amount}")

        followers = await get_active_followers(event.account)
        if not followers:
            return

        # 팔로워별 병렬 복사
        tasks = [self._copy_for_follower(event, f) for f in followers]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _copy_for_follower(self, event: FillEvent, follower):
        follower_id = follower["id"]
        copy_ratio = follower["copy_ratio"]
        max_usd = follower["max_position_usd"]

        try:
            client = PacificaClient(account_address=follower_id)

            # 잔고 조회
            balance = await client.get_balance()
            copy_amount = balance * copy_ratio
            copy_amount = min(copy_amount, max_usd)

            if copy_amount < 1.0:
                log.warning(f"팔로워 {follower_id} 잔고 부족 — 복사 스킵")
                return

            # 복사 주문 실행 (Builder Code 포함)
            order = await client.market_order(
                symbol=event.symbol,
                side=event.side,
                amount=str(round(copy_amount, 2)),
                builder_code=BUILDER_CODE,
            )

            log.info(f"[COPY] follower={follower_id} order={order.get('order_id')} amount={copy_amount}")

            # 거래 로그 저장
            await save_copy_trade({
                "id": str(uuid.uuid4()),
                "trader_id": event.account,
                "follower_id": follower_id,
                "original_order_id": event.order_id,
                "copied_order_id": order.get("order_id"),
                "symbol": event.symbol,
                "side": event.side,
                "trader_amount": float(event.amount),
                "follower_amount": copy_amount,
                "status": "filled" if order.get("order_id") else "failed",
            })

        except Exception as e:
            log.error(f"[COPY FAILED] follower={follower_id} error={e}")
            await save_copy_trade({
                "id": str(uuid.uuid4()),
                "trader_id": event.account,
                "follower_id": follower_id,
                "original_order_id": event.order_id,
                "copied_order_id": None,
                "symbol": event.symbol,
                "side": event.side,
                "trader_amount": float(event.amount),
                "follower_amount": 0,
                "status": "failed",
            })
