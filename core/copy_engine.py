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

from pacifica.client import PacificaClient, BUILDER_CODE, BUILDER_FEE_RATE
from core.alerting import get_alert_manager
from db.database import get_followers, record_copy_trade
from core.retry import retry_sync, classify_error

logger = logging.getLogger(__name__)

# 안전 파라미터
MAX_LEVERAGE = 5
MIN_ORDER_USDC = 10.0   # 최소 주문 금액 (Pacifica testnet min_order_size=$10)
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
        # 팔로워 포지션 추적: {follower: {symbol: {entry_price, size, side}}}
        self._positions: dict[str, dict[str, dict]] = {}
        self._client_cache: dict[str, PacificaClient] = {}
        # 팔로워별 동시 주문 중복 방지 Lock
        self._follower_locks: dict[str, asyncio.Lock] = {}

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
        trader = event.get("account") or event.get("trader_address", "")
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

        # ── 동시 주문 중복 방지 (Lock 획득) ─────────────
        if follower_addr not in self._follower_locks:
            self._follower_locks[follower_addr] = asyncio.Lock()
        lock = self._follower_locks[follower_addr]
        if lock.locked():
            logger.warning(f"[{follower_addr[:8]}] 이전 주문 처리 중 — 중복 주문 스킵")
            return
        async with lock:
            await self._execute_copy(
                follower, follower_addr, copy_ratio, max_pos,
                symbol, side, trader_amount, trader_address, symbol_price
            )

    async def _execute_copy(
        self,
        follower,
        follower_addr: str,
        copy_ratio: float,
        max_pos: float,
        symbol: str,
        side: str,
        trader_amount: str,
        trader_address: str,
        symbol_price: float,
    ) -> None:
        """Lock 획득 후 실제 복사 주문 실행 (내부 메서드)"""
        import math

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
        #    DataCollector 캐시에서 현재 마크 가격 우선 사용, 없으면 이벤트 체결가
        try:
            from core.data_collector import get_price_cache
            cached = get_price_cache().get(symbol, {})
            mark_price = float(cached.get("mark", 0) or 0)
            price_f = mark_price if mark_price > 0 else float(symbol_price if symbol_price > 0 else 0)
        except Exception:
            price_f = float(symbol_price) if symbol_price > 0 else 0.0

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

        # 3. 최소 수량 보장 (MIN_AMOUNT + min_order_size 둘 다 확인)
        # mock_mode: 주문 실행 안 하므로 최솟값 검사 완화 (테스트에서 소액 허용)
        if clamped_amount < MIN_AMOUNT:
            logger.info(f"[{follower_addr[:8]}] 수량 {clamped_amount} < MIN({MIN_AMOUNT}) 스킵")
            return
        # min_order_size(USDC) 보장 — 실제 모드에서만 적용
        if not self.mock_mode and price_f > 0:
            order_usdc_check = clamped_amount * price_f
            _min_usdc = MIN_ORDER_USDC  # $10 (Pacifica testnet 기준)
            if order_usdc_check < _min_usdc:
                logger.info(f"[{follower_addr[:8]}] 주문금액 ${order_usdc_check:.2f} < 최소${_min_usdc} 스킵")
                return

        # 4. lot_size 정렬 (실제 모드에서만 — mock은 API 호출 생략)
        if not self.mock_mode:
            try:
                from core.data_collector import get_price_cache
                mkt = get_price_cache().get(symbol.upper(), {})
                lot = float(mkt.get("lot_size", 0) or 0)
                if lot <= 0:
                    # price_cache miss → Pacifica 마켓 API 직접 조회
                    from pacifica.client import PacificaClient
                    _mc = PacificaClient()
                    markets = _mc.get_markets()
                    m = next((m for m in markets if m.get("symbol") == symbol.upper()), {})
                    lot = float(m.get("lot_size", 0) or 0)
                    # 캐시에 저장
                    if lot > 0 and symbol.upper() in get_price_cache():
                        get_price_cache()[symbol.upper()]["lot_size"] = str(lot)
                if lot > 0:
                    decimals = max(0, -int(math.floor(math.log10(lot))))
                    clamped_amount = round(math.floor(clamped_amount / lot) * lot, decimals)
                    # lot_size >= 1 이면 정수로 (float 정밀도 오류 방지)
                    if lot >= 1.0:
                        clamped_amount = int(clamped_amount)
                    if clamped_amount <= 0:
                        logger.info(f"[{follower_addr[:8]}] lot_size 반올림 후 0 → 스킵")
                        return
            except Exception as e:
                logger.debug(f"lot_size 처리 오류 (무시): {e}")

        copy_amount = str(round(clamped_amount, 8))

        client_order_id = str(uuid.uuid4())
        trade_id = str(uuid.uuid4())

        status = "failed"
        _error_msg: Optional[str] = None
        try:
            # builder_approved(구) 또는 builder_code_approved(신) 둘 중 하나라도 1이면 포함
            # mainnet에서 noivan 승인 완료 → 신규 팔로워는 온보딩 시 approve() 호출
            _bc_approved = (
                (follower["builder_code_approved"] if "builder_code_approved" in follower.keys() else 0) or
                (follower["builder_approved"] if "builder_approved" in follower.keys() else 0)
            )
            bc = BUILDER_CODE if _bc_approved else ""

            if self.mock_mode:
                # Mock 모드: 실제 API 호출 없이 80% 성공 시뮬레이션
                import random
                status = "filled" if random.random() > 0.2 else "failed"
                logger.info(f"[MOCK][{follower_addr[:8]}] {symbol} {side} {copy_amount} → {status}")
            else:
                client = self._get_client(follower_addr)
                # builder_code 포함으로 먼저 시도, "has not approved" 시 없이 재시도
                try:
                    result = retry_sync(
                        client.market_order,
                        symbol=symbol,
                        side=side,
                        amount=copy_amount,
                        slippage_percent=MAX_SLIPPAGE,
                        builder_code=bc,
                        client_order_id=client_order_id,
                        max_retries=2,
                        base_delay=0.5,
                        alert_on_final_fail=False,
                        label=f"{follower_addr[:8]}/{symbol}",
                    )
                except Exception as first_err:
                    err_str = str(first_err)
                    if "has not approved builder code" in err_str and bc:
                        # Builder Code 미승인 → 없이 재시도 (수수료 없이 주문 실행)
                        logger.info(f"[{follower_addr[:8]}] Builder Code 미승인 → 없이 재시도")
                        result = retry_sync(
                            client.market_order,
                            symbol=symbol,
                            side=side,
                            amount=copy_amount,
                            slippage_percent=MAX_SLIPPAGE,
                            builder_code=None,
                            client_order_id=client_order_id,
                            max_retries=2,
                            base_delay=0.5,
                            alert_on_final_fail=True,
                            label=f"{follower_addr[:8]}/{symbol}",
                        )
                    else:
                        raise
                status = "filled" if result.get("data") else "failed"
                logger.info(f"[{follower_addr[:8]}] {symbol} {side} {copy_amount} → {status}")
                if status == "filled":
                    get_alert_manager().order_success(follower_addr, symbol, side, copy_amount)

        except Exception as e:
            logger.error(f"[{follower_addr[:8]}] 주문 실패: {e}")
            status = "failed"
            _error_msg = str(e)
            get_alert_manager().order_failed(follower_addr, symbol, side, _error_msg)

        # Fuul copy_trade 이벤트 (체결 성공 시)
        if status == "filled":
            try:
                from fuul.referral import get_fuul
                amount_usdc = float(copy_amount) * price_f if price_f > 0 else 0
                get_fuul().track_copy_trade(
                    follower_address=follower_addr,
                    trader_address=trader_address,
                    symbol=symbol,
                    side=side,
                    amount_usdc=amount_usdc,
                    order_id=client_order_id,
                )
            except Exception:
                pass

        # Builder Fee 기록 (체결 성공 시)
        if status == "filled" and price_f > 0:
            try:
                amount_usdc = float(copy_amount) * price_f
                # Builder Fee = 주문금액 × 0.001 (0.1%)
                fee_usdc = round(amount_usdc * float(BUILDER_FEE_RATE), 6)
                await self.db.execute(
                    "INSERT INTO fee_records (trade_id, builder_code, fee_usdc, created_at) "
                    "VALUES (?, ?, ?, strftime('%s','now'))",
                    (trade_id, BUILDER_CODE, fee_usdc)
                )
                await self.db.commit()
            except Exception:
                pass

        # PnL 계산 (청산 이벤트 시)
        realized_pnl = None
        exec_price = price_f if price_f > 0 else 0.0
        if status == "filled" and exec_price > 0:
            pos_key = symbol
            follower_positions = self._positions.setdefault(follower_addr, {})
            
            if side in ("bid",):  # 롱 진입 또는 숏 청산
                if pos_key in follower_positions and follower_positions[pos_key].get("side") == "ask":
                    # 숏 포지션 청산 → PnL = (진입가 - 청산가) * size
                    entry = follower_positions[pos_key]["entry_price"]
                    size = float(copy_amount)
                    realized_pnl = (entry - exec_price) * size
                    del follower_positions[pos_key]
                else:
                    # 롱 포지션 진입
                    follower_positions[pos_key] = {"entry_price": exec_price, "size": float(copy_amount), "side": "bid"}
            elif side in ("ask",):  # 숏 진입 또는 롱 청산
                if pos_key in follower_positions and follower_positions[pos_key].get("side") == "bid":
                    # 롱 포지션 청산 → PnL = (청산가 - 진입가) * size
                    entry = follower_positions[pos_key]["entry_price"]
                    size = float(copy_amount)
                    realized_pnl = (exec_price - entry) * size
                    del follower_positions[pos_key]
                else:
                    # 숏 포지션 진입
                    follower_positions[pos_key] = {"entry_price": exec_price, "size": float(copy_amount), "side": "ask"}

        # 기록 (entry_price, exec_price 포함)
        pos_key = symbol
        _entry = None
        if follower_addr in self._positions and pos_key in self._positions[follower_addr]:
            _entry = self._positions[follower_addr][pos_key].get("entry_price")
        await record_copy_trade(self.db, {
            "id": trade_id,
            "follower_address": follower_addr,
            "trader_address": trader_address,
            "symbol": symbol,
            "side": side,
            "amount": copy_amount,
            "price": str(exec_price) if exec_price > 0 else "0",
            "client_order_id": client_order_id,
            "status": status,
            "pnl": realized_pnl,
            "entry_price": _entry,
            "exec_price": exec_price if exec_price > 0 else None,
            "created_at": int(time.time() * 1000),
            "error_msg": _error_msg if status == "failed" else None,
        })


# ── 테스트 ──────────────────────────────────
if __name__ == "__main__":
    import asyncio
    from db.database import init_db, add_trader, add_follower

    async def main():
        db = await init_db(":memory:")
        trader_addr = os.getenv("ACCOUNT_ADDRESS", "")
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

        logger.info("복사 이벤트 처리 중...")
        await engine.on_fill(test_event)
        logger.info("✅ CopyEngine 테스트 완료")
        await db.close()

    asyncio.run(main())
