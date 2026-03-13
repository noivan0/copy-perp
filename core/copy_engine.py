"""
Copy Engine v1 — 트레이더 체결 이벤트 → 팔로워 복사 주문

플로우:
1. PositionMonitor → on_fill(event) 호출
2. CopyEngine이 팔로워 목록 조회
3. 각 팔로워에 대해 비율 계산 → 시장가 복사 주문
4. Builder Code 자동 포함 → 수수료 수취
"""

import asyncio
import logging
import time
import uuid
import json
from typing import Optional

import aiosqlite

from pacifica.client import PacificaClient, BUILDER_CODE
from db.database import get_followers, record_copy_trade
from core.retry import retry_sync, is_retryable

logger = logging.getLogger(__name__)

# 안전 파라미터
MAX_LEVERAGE = 5
MIN_ORDER_USDC = 5.0    # 최소 주문 금액 (미만이면 스킵)
MAX_ORDER_USDC = 5000.0 # 단일 주문 최대 금액 (안전장치)
MAX_SLIPPAGE = "1.0"    # 1% 슬리피지 허용
MIN_AMOUNT = 0.0001     # 최소 수량 (소수점 정밀도)

# 리서치팀 확정 Tier A 트레이더 + 가중치 (copy_ratio 자동 조정)
TIER_A_WEIGHTS: dict[str, float] = {
    'EcX5xSDT45Nvhi2gMTjTnhF3KT2w4sPF54esEZS3hwZu': 0.30,
    '4UBH19qUbXEaqyz9fKrFHuvj8BPMoM87H71s1YPKyGYq':  0.20,
    '7C3sXQ6KvXJLkYGwzjNy2BHpkfEnRHzzfVAgUS64CDEd':  0.20,
    '7gV81bz99MUBVb2aLYxW7MG1RMDdRdJYTPyC2syjba8y':  0.15,
    '3rXoG6i55P7D1Q3tYsB7Unds8nBtKh7vH5VUyMDpWkSe':  0.15,
}


def _parse_side(event_side: str) -> Optional[str]:
    """
    트레이더 체결 side → 팔로워 복사 side
    open_long/fulfill_taker(bid) → "bid"
    open_short/fulfill_taker(ask) → "ask"
    close_long → "ask" (청산), close_short → "bid" (청산)
    """
    mapping = {
        "open_long": "bid",
        "open_short": "ask",
        "close_long": "ask",
        "close_short": "bid",
        "bid": "bid",
        "ask": "ask",
        # position_change 이벤트 side
        "long": "bid",
        "short": "ask",
        # position_closed 이벤트
        "position_change": None,   # event_type만 있는 경우 → side로 재파싱 필요
    }
    result = mapping.get(event_side)
    # 매핑 없어도 None 반환 전에 partial match 시도
    if result is None and event_side:
        if "long" in event_side:
            return "bid"
        if "short" in event_side:
            return "ask"
    return result


class CopyEngine:
    def __init__(self, db: aiosqlite.Connection, mock_mode: bool = False):
        self.db = db
        self.mock_mode = mock_mode
        self._client_cache: dict[str, PacificaClient] = {}

    def _get_client(self, account: str) -> PacificaClient:
        if account not in self._client_cache:
            self._client_cache[account] = PacificaClient(account)
        return self._client_cache[account]

    async def on_fill(self, event: dict) -> None:
        """
        트레이더 체결 이벤트 처리
        event 예시:
          {"event_type": "fulfill_taker", "price": "108.34", "amount": "0.01",
           "side": "open_long", "cause": "normal", "created_at": 1773322044313}
        """
        try:
            await self._process_fill(event)
        except Exception as e:
            logger.error(f"CopyEngine.on_fill 오류: {e}", exc_info=True)

    async def _process_fill(self, event: dict) -> None:
        symbol = event.get("symbol", "BTC")  # WS 이벤트에 symbol 포함 예상
        side_raw = event.get("side", "")
        amount = event.get("amount", "0")
        price = event.get("price", "0")
        trader = event.get("account", "")
        cause = event.get("cause", "normal")

        # 청산 이벤트는 복사 안 함
        if cause == "liquidation":
            logger.info(f"청산 이벤트 스킵: {trader}")
            return

        copy_side = _parse_side(side_raw)
        if not copy_side:
            logger.warning(f"알 수 없는 side: {side_raw}")
            return

        # 팔로워 목록 조회
        followers = await get_followers(self.db, trader)
        if not followers:
            return

        logger.info(f"복사 대상: {len(followers)}명 | {symbol} {copy_side} {amount} @ {price}")

        tasks = [
            self._copy_to_follower(follower, symbol, copy_side, amount, trader, symbol_price=float(price) if price else 0.0)
            for follower in followers
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        ok = sum(1 for r in results if not isinstance(r, Exception))
        fail = len(results) - ok
        logger.info(f"복사 완료: 성공 {ok} / 실패 {fail}")

    async def _copy_to_follower(
        self,
        follower,
        symbol: str,
        side: str,
        trader_amount: str,
        trader_address: str,
        symbol_price: float = 0.0,
    ) -> None:
        follower_addr = follower["address"]
        copy_ratio = float(follower["copy_ratio"])
        max_pos = float(follower["max_position_usdc"])

        # ── 리서치팀 가중치 적용 ──────────────────────────
        # Tier A 트레이더는 사전 정의된 가중치로 copy_ratio 보정
        tier_weight = TIER_A_WEIGHTS.get(trader_address)
        if tier_weight is not None:
            copy_ratio = copy_ratio * tier_weight  # 가중치만큼 실제 복사 비율 조정

        # ── 복사 수량 계산 ────────────────────────────────
        # 1. 비율 적용
        raw_amount = float(trader_amount) * copy_ratio
        clamped_amount = raw_amount

        # 2. USDC 기반 클램핑 (WS 캐시에서 심볼별 실제 가격 있을 때만 적용)
        #    symbol_price는 이벤트 체결가가 아닌 현재 마크 가격이어야 정확함
        #    → WS 캐시 연동 후 활성화 예정 (TODO: api/main.py _ws_cache 연동)
        #    지금은 전역 MAX_ORDER_USDC만 적용 (symbol_price가 해당 심볼 가격일 때만)
        try:
            price_f = float(symbol_price) if symbol_price > 0 else 0.0
        except Exception:
            price_f = 0.0

        if price_f > 0 and symbol_price > 0:
            order_usdc = clamped_amount * price_f

            # 전역 최대 주문 금액 안전장치 (MAX_ORDER_USDC 초과 시만 클램핑)
            if order_usdc > MAX_ORDER_USDC:
                clamped_amount = MAX_ORDER_USDC / price_f
                logger.debug(f"[{follower_addr[:8]}] MAX_ORDER 클램핑: {order_usdc:.2f} → {MAX_ORDER_USDC} USDC")

            # max_position_usdc: 단일 주문이 팔로워 최대 한도를 초과 시만 클램핑
            if order_usdc > max_pos:
                clamped_amount = max_pos / price_f
                logger.debug(f"[{follower_addr[:8]}] max_pos 클램핑: {order_usdc:.2f} → {max_pos} USDC")

        # 3. 최소 수량 보장
        if clamped_amount < MIN_AMOUNT:
            logger.info(f"[{follower_addr[:8]}] 수량 {clamped_amount} < MIN({MIN_AMOUNT}) 스킵")
            return

        copy_amount = str(round(clamped_amount, 6))

        client_order_id = str(uuid.uuid4())
        trade_id = str(uuid.uuid4())

        try:
            bc = BUILDER_CODE if follower["builder_approved"] else ""

            if self.mock_mode:
                # Mock 모드: 실제 API 호출 없이 80% 성공 시뮬레이션
                import random
                status = "filled" if random.random() > 0.2 else "failed"
                logger.info(f"[MOCK][{follower_addr[:8]}] {symbol} {side} {copy_amount} → {status}")
            else:
                client = self._get_client(follower_addr)
                result = retry_sync(
                    client.market_order,
                    symbol=symbol,
                    side=side,
                    amount=copy_amount,
                    slippage_percent=MAX_SLIPPAGE,
                    builder_code=bc,
                    client_order_id=client_order_id,
                    max_retries=2,
                    base_delay=0.3,
                    label=f"{follower_addr[:8]}/{symbol}",
                )
                status = "filled" if result.get("data") else "failed"
                logger.info(f"[{follower_addr[:8]}] {symbol} {side} {copy_amount} → {status}")

        except Exception as e:
            logger.error(f"[{follower_addr[:8]}] 주문 실패: {e}")
            status = "failed"

        # 기록
        await record_copy_trade(self.db, {
            "id": trade_id,
            "follower_address": follower_addr,
            "trader_address": trader_address,
            "symbol": symbol,
            "side": side,
            "amount": copy_amount,
            "price": "0",  # 시장가 — 체결가는 콜백으로 업데이트
            "client_order_id": client_order_id,
            "status": status,
            "created_at": int(time.time() * 1000),
        })


# ── 테스트 ──────────────────────────────────
if __name__ == "__main__":
    import asyncio
    from db.database import init_db, add_trader, add_follower

    async def main():
        db = await init_db(":memory:")
        trader_addr = "3AHZqrocSguMuo9sUUP8G8YN8NwHwWV2DPUQvbDvtfaQ"
        follower_addr = "J5b6Wf5jqh3ck4NyoS6msf37R7KR2owMPLxywrA5YiiT"

        await add_trader(db, trader_addr, "CEO")
        await add_follower(db, follower_addr, trader_addr, copy_ratio=0.5, max_position_usdc=50)

        engine = CopyEngine(db)

        # 테스트 이벤트
        test_event = {
            "account": trader_addr,
            "symbol": "BTC",
            "event_type": "fulfill_taker",
            "price": "100000",
            "amount": "0.01",
            "side": "open_long",
            "cause": "normal",
            "created_at": int(time.time() * 1000),
        }

        print("복사 이벤트 처리 중...")
        await engine.on_fill(test_event)
        print("✅ CopyEngine 테스트 완료")
        await db.close()

    asyncio.run(main())
