"""
Copy Perp — 메인 진입점
트레이더 체결 감지 → 팔로워 자동 복사 → Builder Code 수수료 자동 수취

실행 모드:
  python main.py          → 실제 API 모드 (Agent 바인딩 필요)
  python main.py --mock   → Mock 모드 (API 없이 전체 플로우 검증)
  python main.py --api    → FastAPI 서버만 기동
"""
import asyncio
import logging
import os
import sys
import time
import argparse
import warnings
warnings.filterwarnings("ignore")

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

from db.database import init_db, add_trader, add_follower
from core.copy_engine import CopyEngine
from core.position_monitor import PositionMonitor
from core.data_collector import DataCollector
from core.mock import MOCK_TRADERS, mock_fill_event

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("copy-perp")


async def run_mock_demo(db, engine):
    """Mock 모드 — 실제 API 없이 전체 카피트레이딩 플로우 시연"""
    log.info("=" * 50)
    log.info("MOCK MODE — 전체 플로우 시연")
    log.info("=" * 50)

    trader = MOCK_TRADERS[2]  # FundingArb — 최고 성과
    follower_addr = "MockFollower1111111111111111111111111111111"

    await add_trader(db, trader["address"], trader["alias"])
    await add_follower(db, follower_addr, trader["address"],
                       copy_ratio=1.0, max_position_usdc=100.0)
    log.info(f"트레이더: {trader['alias']} ({trader['address'][:12]}...)")
    log.info(f"팔로워:   {follower_addr[:12]}...")

    log.info("\n--- 시뮬레이션 시작: 10개 체결 이벤트 ---")
    for i in range(10):
        event = mock_fill_event(trader["address"])
        log.info(f"[{i+1}/10] 체결: {event['symbol']} {event['side']} {event['amount']} @ {event['price']}")
        await engine.on_fill(event)
        await asyncio.sleep(0.3)

    # 결과 조회
    async with db.execute(
        "SELECT * FROM copy_trades ORDER BY created_at DESC LIMIT 10"
    ) as cur:
        trades = await cur.fetchall()

    log.info(f"\n--- 결과: {len(trades)}건 복사 거래 기록 ---")
    filled = sum(1 for t in trades if dict(t).get("status") == "filled")
    failed = len(trades) - filled
    log.info(f"성공: {filled} / 실패: {failed}")

    # Fuul 포인트
    from fuul.referral import fuul
    vol = sum(float(dict(t).get("amount", 0)) * 1000 for t in trades)
    await fuul.track_trade_volume(trader["address"], vol)
    pts = fuul.get_points(trader["address"])
    log.info(f"Fuul 포인트 (트레이더): {pts:.2f}")
    log.info(f"레퍼럴 링크: {fuul.generate_referral_link(follower_addr)}")

    log.info("\n✅ Mock 플로우 완료")
    return trades


async def run_data_collector_demo():
    """DataCollector — 실시간 시그널 감지 데모 (5초)"""
    log.info("\n--- DataCollector 시작 (5초) ---")
    collector = DataCollector()

    async def stop_after():
        await asyncio.sleep(5)
        await collector.stop()

    asyncio.create_task(stop_after())
    await collector.start()

    log.info("\n=== 펀딩비 TOP 3 ===")
    for m in collector.get_top_funding(3):
        log.info(f"  {m['symbol']:8} funding={m['funding']:+.5f} OI={m['open_interest']:.0f}")

    log.info("=== Oracle-Mark 괴리 TOP 3 ===")
    for m in collector.get_top_divergence(3):
        log.info(f"  {m['symbol']:8} div={m['divergence']:+.4%} mark={m['mark']:.4f}")

    return collector.market_data


async def run_api_server():
    """FastAPI 서버 기동"""
    import uvicorn
    log.info("FastAPI 서버 기동: http://0.0.0.0:8001")
    config = uvicorn.Config("api.main:app", host="0.0.0.0", port=8001,
                            reload=False, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


async def run_live(db, engine):
    """실제 API 모드 — PositionMonitor로 트레이더 감시"""
    trader = os.getenv("TRADER_ADDRESS") or os.getenv("ACCOUNT_ADDRESS", "")
    follower = os.getenv("ACCOUNT_ADDRESS", "")

    if not follower:
        log.error("ACCOUNT_ADDRESS 환경변수 설정 필요")
        return

    await add_trader(db, trader or follower, "Live Trader")
    await add_follower(db, follower, trader or follower,
                       copy_ratio=float(os.getenv("COPY_RATIO", "0.5")),
                       max_position_usdc=float(os.getenv("MAX_POSITION_USDC", "50")))

    collector = DataCollector(db)
    monitor = PositionMonitor(trader or follower, engine.on_fill)

    log.info("Copy Perp LIVE 가동 — Ctrl+C로 종료")
    try:
        await asyncio.gather(
            collector.start(),
            monitor.start(),
        )
    except asyncio.CancelledError:
        await collector.stop()
        await monitor.stop()


async def main():
    parser = argparse.ArgumentParser(description="Copy Perp")
    parser.add_argument("--mock", action="store_true", help="Mock 모드")
    parser.add_argument("--api", action="store_true", help="FastAPI 서버만")
    parser.add_argument("--data", action="store_true", help="DataCollector 데모")
    args = parser.parse_args()

    db = await init_db()
    engine = CopyEngine(db, mock_mode=args.mock)

    if args.mock:
        await run_mock_demo(db, engine)
        await run_data_collector_demo()
    elif args.api:
        await run_api_server()
    elif args.data:
        await run_data_collector_demo()
    else:
        await run_live(db, engine)

    await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("종료")
