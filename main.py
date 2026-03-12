"""
Copy Perp — 메인 진입점
트레이더 포지션 감시 → 팔로워 자동 복사
"""
import asyncio
import logging
import os
import sys
import warnings
warnings.filterwarnings("ignore")

sys.path.insert(0, os.path.dirname(__file__))

from pacifica.client import PacificaClient, PositionPoller, PriceStream
from core.copy_engine import CopyEngine
from db.models import DB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


async def main():
    db = DB()
    await db.init()
    log.info("DB 초기화 완료")

    def make_client(pk: str) -> PacificaClient:
        return PacificaClient(private_key=pk)

    engine = CopyEngine(db=db, pacifica_client_factory=make_client)

    # ─── 트레이더 등록 ───────────────────────────
    # 환경변수 또는 직접 지정
    TRADER_PK = os.getenv("TRADER_PRIVATE_KEY", "")
    FOLLOWER_PK = os.getenv("FOLLOWER_PRIVATE_KEY", "")

    if not TRADER_PK or not FOLLOWER_PK:
        log.warning("TRADER_PRIVATE_KEY / FOLLOWER_PRIVATE_KEY 미설정 — 시뮬레이션 모드")
        # 테스트용 더미 주소
        trader_id = "EfZBDpotHQcxwaTVjvgys7RFoJBNgYA7T55SBzK4FKt4"
        follower_id = "5GCnPg6tLD1fe52WLDKWx6CSCkppTeDJce3vZytaQVEn"
    else:
        trader_client = PacificaClient(private_key=TRADER_PK)
        follower_client = PacificaClient(private_key=FOLLOWER_PK)
        trader_id = trader_client.account
        follower_id = follower_client.account

    await db.add_trader(trader_id, alias="주트레이더")
    await db.add_follower(
        follower_id=follower_id,
        trader_id=trader_id,
        private_key=FOLLOWER_PK,
        copy_ratio=0.5,          # 팔로워 잔고의 50% 복사
        max_position_usd=50,     # 최대 $50
    )
    log.info(f"트레이더: {trader_id}")
    log.info(f"팔로워:   {follower_id}")

    # ─── 포지션 폴러 ─────────────────────────────
    trader_client_readonly = PacificaClient(account_address=trader_id)

    async def on_position_change(change: dict):
        log.info(f"[EVENT] {change['type']} {change['side']} {change['symbol']} Δ{change['size_delta']}")
        await engine.on_position_change(change, trader_id)

    poller = PositionPoller(trader_client_readonly, on_change=on_position_change)

    # ─── 가격 스트림 ──────────────────────────────
    price_stream = PriceStream()

    async def on_price_update(items):
        # 향후 신호 분석에 활용
        pass

    price_stream.on_update = on_price_update

    log.info("Copy Perp 시작 — 트레이더 포지션 감시 중...")

    await asyncio.gather(
        poller.start(interval=0.5),
        price_stream.start(),
    )


if __name__ == "__main__":
    asyncio.run(main())
