"""
Copy Perp — 메인 진입점
트레이더 체결 감지(REST 폴링 500ms) → 팔로워 자동 복사 → Builder Code 수수료
"""
import asyncio
import logging
import os
import sys
import warnings
warnings.filterwarnings("ignore")

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from db.database import init_db, add_trader, add_follower
from core.copy_engine import CopyEngine
from core.position_monitor import PositionMonitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── 설정 ─────────────────────────────────────────────
TRADER_ADDRESS  = os.getenv("TRADER_ADDRESS",  "")   # 복사할 트레이더 지갑
FOLLOWER_ADDRESS = os.getenv("ACCOUNT_ADDRESS", "")  # 팔로워 = 우리 계정
COPY_RATIO      = float(os.getenv("COPY_RATIO", "0.5"))
MAX_POSITION_USDC = float(os.getenv("MAX_POSITION_USDC", "50"))


async def main():
    # ── DB 초기화 ─────────────────────────────────
    db = await init_db()
    log.info("DB 초기화 완료")

    # ── 트레이더/팔로워 등록 ─────────────────────
    trader = TRADER_ADDRESS or FOLLOWER_ADDRESS  # 데모: 자기 자신 모니터링
    follower = FOLLOWER_ADDRESS

    if not follower:
        log.error("ACCOUNT_ADDRESS 환경변수를 설정하세요")
        return

    await add_trader(db, trader, alias="Target Trader")
    await add_follower(db, follower, trader, copy_ratio=COPY_RATIO, max_position_usdc=MAX_POSITION_USDC)
    log.info(f"트레이더: {trader[:16]}...")
    log.info(f"팔로워:   {follower[:16]}...")

    # ── Copy Engine ────────────────────────────────
    engine = CopyEngine(db)

    # ── Position Monitor (REST 폴링) ──────────────
    monitor = PositionMonitor(trader, engine.on_fill)

    log.info("Copy Perp 가동 — Ctrl+C로 종료")
    try:
        await monitor.start()
    except KeyboardInterrupt:
        await monitor.stop()
        log.info("종료")


if __name__ == "__main__":
    asyncio.run(main())
