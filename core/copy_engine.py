"""
Copy Engine v1.2 — 트레이더 체결 이벤트 → 팔로워 복사 주문

플로우:
1. PositionMonitor → on_fill(event) 호출
2. CopyEngine이 팔로워 목록 조회
3. 각 팔로워에 대해 비율 계산 → 시장가 복사 주문
4. Builder Code 자동 포함 → 수수료 수취

v1.1 수정:
- _parse_side: 대소문자 정규화, None 입력 안전 처리
- _execute_copy: 음수 수량 방어, price_f=0 시 MIN_ORDER_USDC 체크 우회 수정
- builder_code_approved 접근 패턴 .get()으로 통일

v1.2 수정 (R11):
- 동일 심볼 LONG+SHORT 동시 신호 스킵 (헤지 포지션 방지)
- 잔액 부족 시 copy_trades에 status='skipped_insufficient' 기록
- _pending_directions: 처리 중 방향 추적 버퍼 (5초 TTL)
"""

import asyncio
import logging
import os
import time
import uuid
import json
from typing import Optional

from pacifica.client import PacificaClient, BUILDER_CODE, BUILDER_FEE_RATE
from core.alerting import get_alert_manager
from db.database import (
    get_followers, record_copy_trade,
    upsert_follower_position, get_follower_position,
    delete_follower_position, get_all_follower_positions,
)
from core.retry import retry_sync, classify_error
try:
    from core.data_collector import get_price_cache
except ImportError:
    def get_price_cache(): return {}
try:
    from core.strategy import (
        calc_stop_price, should_stop,
        SUPPORTED_SYMBOLS,
    )
    from core.strategy_presets import get_preset as get_strategy_preset
except ImportError:
    calc_stop_price = None
    should_stop = None
    SUPPORTED_SYMBOLS = set()
    get_strategy_preset = None

logger = logging.getLogger(__name__)

# 안전 파라미터
MAX_LEVERAGE = 5
MIN_ORDER_USDC = 10.0   # 최소 주문 금액 (Pacifica testnet min_order_size=$10)
MAX_ORDER_USDC = 5000.0 # 단일 주문 최대 금액 (안전장치)
MAX_SLIPPAGE = "1.0"    # 1% 슬리피지 허용
MIN_AMOUNT = 0.0001     # 최소 수량 (소수점 정밀도)

# 메인넷 확정 Tier A/S 트레이더 + 가중치 (2026-03-19 신뢰도 기반)
TIER_A_WEIGHTS: dict[str, float] = {
    # S등급
    "EcX5xSDT45Nvhi2gMTjTnhF3KT2w4sPF54esEZS3hwZu": 0.30,   # trust 74.5, ROI 82.5%
    # A등급 — 신뢰도 순
    "A6VY4ZBUohgSLkwMuDwDvAnzgiXFB1eTDzaixyitPJep":  0.12,   # trust 58.2, ROI 58.9%
    "4UBH19qUbXEaqyz9fKrFHuvj8BPMoM87H71s1YPKyGYq":  0.12,   # trust 61.9, ROI 58.8%
    "7gV81bz99MUBVb2aLYxW7MG1RMDdRdJYTPyC2syjba8y":  0.12,   # trust 61.0, ROI 51.5%
    "3rXoG6i55P7D1Q3tYsB7Unds8nBtKh7vH5VUyMDpWkSe":  0.10,   # trust 60.8, ROI 47.4%
    "E1vabqxiuUfBQKaH8L3P1tDvxG5mMj7nRkC2sQwYzXe9":  0.08,   # trust 58.2, ROI 47.6%
    "5BPd5WYVvDE2tXg3aKj9mPqR7nLhB4cF8vZsWuYeC1Nd":  0.08,   # trust 59.9, ROI 43.6%
    "9XCVb4SQVADNkLmP2rTgB5jHuF3wEzXc8nQsYvD7eAi":   0.08,   # trust 58.7, ROI 43.5%
}


def _parse_side(event_side) -> Optional[str]:  # type-checked
    """
    트레이더 체결 side → 팔로워 복사 side
    open_long/fulfill_taker(bid) → "bid"
    open_short/fulfill_taker(ask) → "ask"
    close_long → "ask" (청산), close_short → "bid" (청산)
    """
    if not event_side:
        return None
    # 대소문자 정규화 (API가 OPEN_LONG 등 대문자로 보낼 수 있음)
    normalized = str(event_side).lower().strip()
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
    result = mapping.get(normalized)
    # 매핑 없어도 None 반환 전에 partial match 시도
    if result is None:
        if "long" in normalized:
            return "bid"
        if "short" in normalized:
            return "ask"
    return result


class CopyEngine:
    def __init__(self, db, mock_mode: bool = False):  # db: TursoDb or aiosqlite.Connection
        self.db = db
        self.mock_mode = mock_mode
        # 팔로워 포지션 추적: {follower: {symbol: {entry_price, size, side}}}
        self._positions: dict[str, dict[str, dict]] = {}
        self._client_cache: dict[str, PacificaClient] = {}
        # 팔로워별 동시 주문 중복 방지 Lock
        self._follower_locks: dict[str, asyncio.Lock] = {}
        # R11: 동일 심볼 동시 방향 추적 버퍼 (LONG+SHORT 동시 신호 스킵용)
        # {symbol: {"directions": set(), "ts": float}}
        self._pending_directions: dict[str, dict] = {}
        # R11: 마지막 이벤트 처리 시간 (queue stall 감지용)
        self._last_processed_ts: float = 0.0

    async def _load_positions_from_db(self) -> None:
        """서버 재시작 시 DB의 follower_positions → self._positions 복원"""
        try:
            async with self.db.execute("SELECT * FROM follower_positions") as cur:
                rows = await cur.fetchall()
            for row in rows:
                r = dict(row)
                follower_addr = r["follower_address"]
                symbol = r["symbol"]
                if follower_addr not in self._positions:
                    self._positions[follower_addr] = {}
                self._positions[follower_addr][symbol] = {
                    "entry_price": r["entry_price"],
                    "size": r["size"],
                    "side": r["side"],
                    # R10: SL/TP/high_price/strategy 복원 (재시작 후 StopLossMonitor 정확도)
                    "stop_loss_price": r.get("stop_loss_price", 0) or 0,
                    "take_profit_price": r.get("take_profit_price", 0) or 0,
                    "high_price": r.get("high_price") or r["entry_price"],
                    "strategy": r.get("strategy", "passive") or "passive",
                }
            logger.info(f"[PnL] DB에서 포지션 복원 완료: {len(rows)}건")
        except Exception as e:
            logger.warning(f"[PnL] 포지션 복원 오류 (무시): {e}")

    async def _get_trader_stats_sync(self, trader_address: str) -> dict | None:
        """mainnet_stats에서 트레이더 통계 조회 (Kelly 계산용)"""
        try:
            async with self.db.execute("""
                SELECT win_rate, payoff_ratio, kelly, profit_factor, closed_cnt, carp_score
                FROM mainnet_stats
                WHERE trader_address = ?
                ORDER BY snapshot_ts DESC LIMIT 1
            """, (trader_address,)) as cur:
                row = await cur.fetchone()
                if row:
                    cols = [d[0] for d in cur.description]
                    return dict(zip(cols, row))
        except Exception:
            pass
        # mainnet_stats 없으면 traders 테이블 fallback
        try:
            async with self.db.execute("""
                SELECT win_rate, sharpe as kelly FROM traders WHERE address = ?
            """, (trader_address,)) as cur:
                row = await cur.fetchone()
                if row:
                    return {"win_rate": row[0], "payoff_ratio": 1.5, "kelly": row[1], "closed_cnt": 50}
        except Exception:
            pass
        return None

    def _get_client(self, account: str) -> PacificaClient:  # type-checked
        if account not in self._client_cache:
            self._client_cache[account] = PacificaClient(account)
        return self._client_cache[account]

    async def on_fill(self, event: dict) -> None:  # type-checked
        """
        트레이더 체결 이벤트 처리
        event 예시:
          {"event_type": "fulfill_taker", "price": "108.34", "amount": "0.01",
           "side": "open_long", "cause": "normal", "created_at": 1773322044313}
        """
        try:
            await self._process_fill(event)
            self._last_processed_ts = time.time()  # R11: 처리 시간 갱신
        except Exception as e:
            logger.error(f"CopyEngine.on_fill 오류: {e}", exc_info=True)

    async def _process_fill(self, event: dict) -> None:  # type-checked
        symbol = event.get("symbol")  # WS 이벤트에 symbol 필수 포함
        if not symbol:
            logger.warning(f"[CopyEngine] symbol 누락 이벤트 스킵: {event}")
            return
        side_raw = event.get("side", "")
        amount = event.get("amount", "0")
        price = event.get("price", "0")
        trader = event.get("account") or event.get("trader_address", "")
        cause = event.get("cause", "normal")

        # amount/price float 변환 안전 처리
        try:
            _amount_f = float(amount)
        except (TypeError, ValueError):
            logger.warning(f"[CopyEngine] amount 변환 실패: {amount!r} — 이벤트 스킵")
            return
        try:
            _price_f = float(price) if price else 0.0
        except (TypeError, ValueError):
            _price_f = 0.0

        # 청산 이벤트는 복사 안 함
        if cause == "liquidation":
            logger.info(f"청산 이벤트 스킵: {trader}")
            return

        copy_side = _parse_side(side_raw)
        if not copy_side:
            logger.warning(f"알 수 없는 side: {side_raw}")
            return

        # ── R11: 동일 심볼 LONG+SHORT 동시 신호 스킵 ──────────────────────
        # open_long + open_short 동시에 오면 헤지 포지션 → 수수료만 이중 발생
        # 청산 신호(close_long/close_short)는 정상 처리
        _is_open_signal = side_raw.lower() in ("open_long", "open_short", "bid", "ask") and \
                          side_raw.lower() not in ("close_long", "close_short")
        # 실제 close 이벤트는 _parse_side 결과만으로는 open/close 구분 불가 → 원본 side_raw 사용
        _raw_lower = str(side_raw).lower()
        _is_open_signal = _raw_lower.startswith("open_") or _raw_lower in ("bid", "ask", "long", "short")

        if _is_open_signal and symbol:
            _now_ts = time.time()
            _TTL = 5.0  # 5초 내 동시 신호 감지 윈도우
            _pdir = self._pending_directions.get(symbol)
            if _pdir and (_now_ts - _pdir["ts"]) < _TTL:
                # 이미 같은 심볼에 다른 방향 신호가 등록됨 → 헤지 스킵
                _existing_sides = _pdir["directions"]
                _opposite = {"bid", "ask"}
                if copy_side in _existing_sides:
                    # 같은 방향 중복 — 정상 진행
                    pass
                elif _opposite - {copy_side} & _existing_sides:
                    # 반대 방향 존재 → 스킵
                    logger.warning(
                        f"[CopyEngine] {symbol} 동시 LONG+SHORT 신호 감지 — 헤지 포지션 방지: "
                        f"기존={_existing_sides} 신규={copy_side} → 스킵"
                    )
                    return
            # 현재 신호 방향 등록
            if symbol not in self._pending_directions or (_now_ts - self._pending_directions[symbol]["ts"]) >= _TTL:
                self._pending_directions[symbol] = {"directions": set(), "ts": _now_ts}
            self._pending_directions[symbol]["directions"].add(copy_side)
            # 오래된 버퍼 정리 (메모리 누수 방지)
            if len(self._pending_directions) > 200:
                _to_del = [k for k, v in list(self._pending_directions.items())
                           if (_now_ts - v["ts"]) >= _TTL]
                for k in _to_del:
                    self._pending_directions.pop(k, None)

        # 팔로워 목록 조회
        followers = await get_followers(self.db, trader)
        if not followers:
            return

        logger.info(f"복사 대상: {len(followers)}명 | {symbol} {copy_side} {amount} @ {price}")

        tasks = [
            self._copy_to_follower(
                follower, symbol, copy_side, amount, trader,
                symbol_price=_price_f
            )
            for follower in followers
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        ok = sum(1 for r in results if not isinstance(r, Exception))
        fail = len(results) - ok
        # P1 Fix (Round 7): 개별 팔로워 오류 상세 로깅 (루프 외부에서 한번에 처리)
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                _follower_addr_i = followers[i].get("address", "??")[:8] if isinstance(followers[i], dict) else "??"
                logger.error(f"[CopyEngine] 팔로워 [{_follower_addr_i}] 처리 예외: {r}", exc_info=False)
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
        # P1 Fix (Round 5): aiosqlite.Row는 .get()을 지원하지 않음 → dict 변환
        # (이후 _execute_copy에서 follower.get() 호출 안전 보장)
        if not isinstance(follower, dict):
            try:
                follower = dict(follower)
            except Exception:
                follower = {k: follower[k] for k in follower.keys()} if hasattr(follower, 'keys') else {}
        follower_addr = follower["address"]
        try:
            copy_ratio = float(follower["copy_ratio"])
            max_pos = float(follower["max_position_usdc"])
        except (TypeError, ValueError) as _e:
            logger.warning(f"[{follower['address'][:8]}] copy_ratio/max_pos 변환 실패: {_e} — 기본값 사용")
            copy_ratio = 0.1
            max_pos = 100.0

        # ── 동시 주문 중복 방지 (Lock 획득) ─────────────
        # R10: per-follower+symbol lock (기존 per-follower는 다른 심볼도 블록하는 과도한 직렬화)
        # R10b: lock 해제 후 삭제 — 메모리 누수 방지 (팔로워×심볼 조합 무한 증가)
        lock_key = f"{follower_addr}:{symbol}"
        if lock_key not in self._follower_locks:
            self._follower_locks[lock_key] = asyncio.Lock()
        lock = self._follower_locks[lock_key]
        if lock.locked():
            logger.warning(f"[{follower_addr[:8]}] {symbol} 이전 주문 처리 중 — 중복 주문 스킵")
            return
        async with lock:
            await self._execute_copy(
                follower, follower_addr, copy_ratio, max_pos,
                symbol, side, trader_amount, trader_address, symbol_price
            )
        # lock 완전 해제 후 cleanup (메모리 누수 방지)
        # dict 크기가 500 초과하면 비사용 lock 정리
        if len(self._follower_locks) > 500:
            to_del = [k for k, v in list(self._follower_locks.items()) if not v.locked()]
            for k in to_del:
                self._follower_locks.pop(k, None)

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

        # ── 전략 프리셋 로드 ──────────────────────────────
        try:
            strategy_id = follower["strategy"] or "default"
        except (KeyError, IndexError, TypeError):
            strategy_id = "default"
        # ✅ None 가드: import 실패 시 기본값 사용
        preset = get_strategy_preset(strategy_id) if get_strategy_preset is not None else {}

        # ── 심볼 필터: FX/미지원 종목 사전 차단 (mock_mode 제외) ──────────
        if preset.get("symbol_filter", True) and not self.mock_mode:
            _supported = SUPPORTED_SYMBOLS if SUPPORTED_SYMBOLS else set()
            if _supported and symbol.upper() not in _supported:
                logger.info(f"[{follower_addr[:8]}] {symbol} 미지원 심볼 차단 (strategy={strategy_id})")
                return

        # ── P0 Fix (Round 4): volume=0 + 극단 펀딩비 마켓 진입 차단 ──────
        # PIPPIN 같은 유동성 없는 마켓: volume_24h=0 + |funding| > 3% 이면 주문 스킵
        # (슬리피지 폭발 위험, 사실상 진입/청산 불가)
        if not self.mock_mode:
            try:
                _mkt = get_price_cache().get(symbol.upper(), {})
                _vol24 = float(_mkt.get("volume_24h", 0) or _mkt.get("volume", 0) or 0)
                _funding = float(_mkt.get("funding", 0) or 0)
                _FUNDING_THRESHOLD = 0.03   # |funding| > 3% = 극단 펀딩비
                if _vol24 == 0 and abs(_funding) > _FUNDING_THRESHOLD:
                    logger.warning(
                        f"[{follower_addr[:8]}] {symbol} 주문 스킵: "
                        f"volume_24h=0 + funding={_funding:.2%} (극단 펀딩비 + 유동성 없음)"
                    )
                    return
            except Exception as _ve:
                logger.debug(f"[{follower_addr[:8]}] volume/funding 체크 오류 (무시): {_ve}")

        # ── 전략 프리셋 파라미터 적용 ────────────────────
        # P0 Fix (Round 6): 프리셋의 copy_ratio는 팔로워 개별 설정의 상한선으로만 사용
        # (프리셋이 팔로워 설정을 완전히 덮어쓰는 버그 수정)
        # 팔로워 DB 설정값(copy_ratio)이 존재하면 이를 우선, 프리셋은 최대값으로 제한
        _preset_ratio = preset.get("copy_ratio")
        if _preset_ratio is not None:
            # 프리셋의 copy_ratio는 팔로워 설정의 상한선: min(follower_ratio, preset_ratio)
            copy_ratio = min(copy_ratio, float(_preset_ratio))
        _preset_max_pos = preset.get("max_position_usdc")
        if _preset_max_pos is not None:
            # 프리셋의 max_position_usdc는 팔로워 설정의 상한선
            max_pos = min(max_pos, float(_preset_max_pos))

        # ── 트레이더 통계 조회 (로깅용) ──────────────────
        trader_stats = await self._get_trader_stats_sync(trader_address)

        # ── 복사 수량 계산 ────────────────────────────────
        # 1. 비율 적용
        raw_amount = float(trader_amount) * copy_ratio
        clamped_amount = raw_amount

        # ── P1 Fix (Round 5): MAX_LEVERAGE 클램핑 ─────────────────────────
        # Pacifica는 margin 계정이므로 MAX_LEVERAGE × 마진 = 최대 포지션
        # max_position_usdc를 MAX_LEVERAGE × 단일 포지션 마진 기준으로 간접 제한
        # (실제 leverage 파라미터는 market_order에 없음 — amount × price 상한으로 제어)
        # max_position_usdc가 이미 설정되어 있으므로, 추가로 per-position cap 강제:
        #   effective_max_pos = min(max_pos, MAX_LEVERAGE × min_margin_usdc)
        # Round 5: max_pos 자체를 MAX_LEVERAGE 기반 상한으로 재클램핑
        _leverage_cap_usdc = MAX_LEVERAGE * max_pos  # 보수적: max_pos를 마진 기준으로 재해석
        # 더 정확한 방식: max_pos = 마진 × leverage → max_pos는 이미 포지션 크기
        # 즉 max_position_usdc = 전체 포지션 USDC, leverage cap = MAX_ORDER_USDC × MAX_LEVERAGE
        # → MAX_ORDER_USDC(5000) × MAX_LEVERAGE(5) = 25000이므로 max_pos가 이미 낮음
        # 실질적 클램핑: max_pos를 MAX_LEVERAGE × 합리적 마진($200) = $1000으로 추가 제한
        _per_position_leverage_cap = MAX_LEVERAGE * 200.0  # $200 마진 × 5x = $1000 최대 포지션
        if max_pos > _per_position_leverage_cap:
            max_pos = _per_position_leverage_cap
            logger.debug(f"[{follower_addr[:8]}] MAX_LEVERAGE({MAX_LEVERAGE}x) 적용: max_pos 클램핑 → ${max_pos:.0f}")

        # 2. USDC 기반 클램핑 (WS 캐시에서 심볼별 실제 가격 있을 때만 적용)
        #    DataCollector 캐시에서 현재 마크 가격 우선 사용, 없으면 이벤트 체결가
        try:
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

        # 3. 음수 수량 방어 (copy_ratio 음수 등 이상 입력)
        if clamped_amount <= 0:
            logger.info(f"[{follower_addr[:8]}] 수량 {clamped_amount} <= 0 — 스킵")
            return

        # 4. 최소 수량 보장 (MIN_AMOUNT + min_order_size 둘 다 확인)
        # mock_mode: 주문 실행 안 하므로 최솟값 검사 완화 (테스트에서 소액 허용)
        if clamped_amount < MIN_AMOUNT:
            logger.info(f"[{follower_addr[:8]}] 수량 {clamped_amount} < MIN({MIN_AMOUNT}) 스킵")
            return
        # min_order_size(USDC) 보장 — 실제 모드에서만 적용
        # price_f=0인 경우에도 체크: amount 자체가 MIN_AMOUNT 이상이면 경고 후 진행
        if not self.mock_mode:
            if price_f > 0:
                order_usdc_check = clamped_amount * price_f
                _min_usdc = MIN_ORDER_USDC  # $10 (Pacifica testnet 기준)
                if order_usdc_check < _min_usdc:
                    logger.info(f"[{follower_addr[:8]}] 주문금액 ${order_usdc_check:.2f} < 최소${_min_usdc} 스킵")
                    return
            else:
                # price_f=0: 마크 가격 미수신 상태 → 안전을 위해 주문 보류
                logger.warning(f"[{follower_addr[:8]}] {symbol} 마크 가격 미수신(0) — 주문 보류")
                return

        # 4. lot_size 정렬 (실제 모드에서만 — mock은 API 호출 생략)
        if not self.mock_mode:
            try:
                mkt = get_price_cache().get(symbol.upper(), {})
                lot = float(mkt.get("lot_size", 0) or 0)
                if lot <= 0:
                    # price_cache miss → /info API 직접 조회 (PacificaClient 불필요)
                    import requests as _rq, urllib3 as _ul3
                    _ul3.disable_warnings()
                    _iurl = "https://do5jt23sqak4.cloudfront.net/api/v1/info"
                    _ihdr = {"Host": "api.pacifica.fi"}
                    _ir = _rq.get(_iurl, headers=_ihdr, verify=False, timeout=5)
                    if _ir.ok:
                        _info = _ir.json().get("data", [])
                        for _m in _info:
                            _sym = _m.get("symbol","")
                            _ls = float(_m.get("lot_size", 0) or 0)
                            if _sym not in get_price_cache():
                                get_price_cache()[_sym] = {}
                            get_price_cache()[_sym]["lot_size"] = str(_ls)
                        mkt = get_price_cache().get(symbol.upper(), {})
                        lot = float(mkt.get("lot_size", 0) or 0)
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
            # .get()으로 통일 (sqlite3.Row와 dict 모두 지원)
            _bc_approved = (
                follower.get("builder_code_approved", 0) or
                follower.get("builder_approved", 0)
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
                if result.get("data"):
                    status = "filled"
                else:
                    status = "failed"
                    _error_msg = f"Order response missing data: {str(result)[:120]}"
                    logger.warning(f"[{follower_addr[:8]}] {symbol} 주문 응답 data 없음: {result}")
                logger.info(f"[{follower_addr[:8]}] {symbol} {side} {copy_amount} → {status}")
                if status == "filled":
                    get_alert_manager().order_success(follower_addr, symbol, side, copy_amount)

        except Exception as e:
            err_str = str(e)
            # P0 Fix (Round 7): 잔액 부족 에러 분류 — 다른 에러와 구분하여 로깅
            _INSUFFICIENT_PATTERNS = (
                "insufficient", "not enough", "margin", "funds",
                "balance", "exceeds", "below minimum",
            )
            _is_balance_error = any(p in err_str.lower() for p in _INSUFFICIENT_PATTERNS)
            # R11+: 팔로워 자동 일시 중지 (반복 실패 방지)
            # 1) unauthorized to sign: Agent Binding 미완료
            # 2) IP not whitelisted: Pacifica API Key IP whitelist 미설정 (서버 전체 이슈)
            _is_unauthorized = "unauthorized to sign" in err_str.lower() or (
                "unauthorized" in err_str.lower() and "sign on behalf" in err_str.lower()
            )
            _is_ip_blocked = "not whitelisted" in err_str.lower() or (
                "ip address" in err_str.lower() and "whitelisted" in err_str.lower()
            )
            if _is_ip_blocked:
                # IP whitelist 문제: 서버 전체 이슈 — 팔로워 비활성화 불필요, 경고만
                logger.error(
                    f"[{follower_addr[:8]}] IP whitelist 미설정 — Pacifica 대시보드에서 "
                    f"74.220.48.248 추가 필요: {err_str[:120]}"
                )
                status = "skipped_ip_blocked"
            elif _is_unauthorized:
                logger.warning(
                    f"[{follower_addr[:8]}] Agent 미승인 — 팔로워 자동 중지 "
                    f"(Pacifica 앱에서 Agent Binding 필요): {err_str[:120]}"
                )
                # 팔로워 비활성화: 같은 에러 반복 방지
                try:
                    await self.db.execute(
                        "UPDATE followers SET active=0 WHERE address=?",
                        (follower_addr,)
                    )
                    await self.db.commit()
                    logger.info(f"[{follower_addr[:8]}] 팔로워 비활성화 완료 (agent_binding_required)")
                except Exception as _db_err:
                    logger.debug(f"팔로워 비활성화 실패 (무시): {_db_err}")
                status = "skipped_agent_unbound"
            elif _is_balance_error:
                logger.warning(
                    f"[{follower_addr[:8]}] 잔액 부족 — {symbol} {side} {copy_amount}: {err_str[:120]}"
                )
                # R11: 잔액 부족은 별도 status로 기록 (단순 failed와 구분)
                status = "skipped_insufficient"
            else:
                logger.error(f"[{follower_addr[:8]}] 주문 실패: {e}")
                status = "failed"
            _error_msg = err_str
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
            except Exception as e:
                logger.debug(f"무시된 예외: {e}")

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
            except Exception as e:
                logger.debug(f"무시된 예외: {e}")

        # ── PnL 계산 + follower_positions DB 영속화 ──────────────
        exec_price = price_f if price_f > 0 else 0.0
        realized_pnl = None

        if status == "filled" and exec_price > 0:
            pos_key = symbol
            follower_positions = self._positions.setdefault(follower_addr, {})

            # DB에서 포지션 fallback 조회 (메모리에 없을 때)
            if pos_key not in follower_positions:
                try:
                    db_pos = await get_follower_position(self.db, follower_addr, pos_key)
                    if db_pos:
                        follower_positions[pos_key] = {
                            "entry_price": db_pos["entry_price"],
                            "size": db_pos["size"],
                            "side": db_pos["side"],
                        }
                except Exception as _e:
                    logger.debug(f"[PnL] DB 포지션 조회 오류 (무시): {_e}")

            if side == "bid":  # 롱 진입 또는 숏 청산
                if pos_key in follower_positions and follower_positions[pos_key].get("side") == "ask":
                    # 숏 포지션 청산 → PnL = (진입가 - 청산가) × size
                    entry = follower_positions[pos_key]["entry_price"]
                    size = float(copy_amount)
                    realized_pnl = round((entry - exec_price) * size, 6)
                    del follower_positions[pos_key]
                    try:
                        await delete_follower_position(self.db, follower_addr, pos_key)
                    except Exception as _e:
                        logger.debug(f"[PnL] 포지션 삭제 오류 (무시): {_e}")
                    logger.info(f"[PnL] {follower_addr[:8]} {symbol} 숏청산 PnL={realized_pnl:+.4f}")
                elif pos_key in follower_positions and follower_positions[pos_key].get("side") == "bid":
                    # ── P0 Fix (Round 7): 같은 심볼에 OPEN_LONG이 2번 오는 경우 ──
                    # 기존 롱 포지션이 이미 열려있으면 중복 진입 방지
                    # Pacifica는 자동으로 포지션을 증가(add)하므로 size만 업데이트
                    _existing = follower_positions[pos_key]
                    _old_size = _existing.get("size", 0)
                    _new_size = _old_size + float(copy_amount)
                    _old_entry = _existing.get("entry_price", exec_price)
                    # 가중 평균 진입가 계산 (포지션 추가 시)
                    _avg_entry = round((_old_entry * _old_size + exec_price * float(copy_amount)) / _new_size, 8) if _new_size > 0 else exec_price
                    follower_positions[pos_key]["size"] = _new_size
                    follower_positions[pos_key]["entry_price"] = _avg_entry
                    logger.info(
                        f"[PnL] {follower_addr[:8]} {symbol} 롱 포지션 추가: "
                        f"size {_old_size}→{_new_size}, avg_entry={_avg_entry:.4f}"
                    )
                    try:
                        await upsert_follower_position(
                            self.db, follower_addr, pos_key, "bid", _avg_entry, _new_size
                        )
                    except Exception as _e:
                        logger.debug(f"[PnL] 포지션 업데이트 오류 (무시): {_e}")
                else:
                    # 롱 포지션 신규 진입 — stop/take 계산 (strategy_presets 파라미터 기반)
                    from core.strategy import _calc_stop_from_preset
                    sl_price, tp_price = _calc_stop_from_preset(exec_price, "bid", preset)
                    follower_positions[pos_key] = {
                        "entry_price": exec_price,
                        "size": float(copy_amount),
                        "side": "bid",
                        "high_price": exec_price,
                        "stop_loss_price": sl_price or 0,
                        "take_profit_price": tp_price or 0,
                        "strategy": strategy_id,
                    }
                    try:
                        await upsert_follower_position(
                            self.db, follower_addr, pos_key, "bid", exec_price, float(copy_amount),
                            stop_loss_price=sl_price or 0,
                            take_profit_price=tp_price or 0,
                            high_price=exec_price,
                            strategy=strategy_id,
                        )
                        await self.db.execute(
                            "UPDATE positions SET high_price=?, stop_loss_price=?, take_profit_price=?, strategy=? "
                            "WHERE follower_address=? AND symbol=? AND status='open'",
                            (exec_price, sl_price or 0, tp_price or 0, strategy_id, follower_addr, pos_key)
                        )
                        await self.db.commit()
                        if sl_price:
                            logger.info(f"[SL] {follower_addr[:8]} {symbol} 롱 SL=${sl_price:.4f}")
                    except Exception as _e:
                        logger.debug(f"[PnL] 포지션 저장 오류 (무시): {_e}")

            elif side == "ask":  # 숏 진입 또는 롱 청산
                if pos_key in follower_positions and follower_positions[pos_key].get("side") == "bid":
                    # 롱 포지션 청산 → PnL = (청산가 - 진입가) × size
                    entry = follower_positions[pos_key]["entry_price"]
                    size = float(copy_amount)
                    realized_pnl = round((exec_price - entry) * size, 6)
                    del follower_positions[pos_key]
                    try:
                        await delete_follower_position(self.db, follower_addr, pos_key)
                    except Exception as _e:
                        logger.debug(f"[PnL] 포지션 삭제 오류 (무시): {_e}")
                    logger.info(f"[PnL] {follower_addr[:8]} {symbol} 롱청산 PnL={realized_pnl:+.4f}")
                elif pos_key in follower_positions and follower_positions[pos_key].get("side") == "ask":
                    # ── P0 Fix (Round 7): 같은 심볼에 OPEN_SHORT가 2번 오는 경우 ──
                    # 기존 숏 포지션이 이미 열려있으면 포지션 추가 (사이즈 + 가중 평균 진입가)
                    _existing = follower_positions[pos_key]
                    _old_size = _existing.get("size", 0)
                    _new_size = _old_size + float(copy_amount)
                    _old_entry = _existing.get("entry_price", exec_price)
                    _avg_entry = round((_old_entry * _old_size + exec_price * float(copy_amount)) / _new_size, 8) if _new_size > 0 else exec_price
                    follower_positions[pos_key]["size"] = _new_size
                    follower_positions[pos_key]["entry_price"] = _avg_entry
                    logger.info(
                        f"[PnL] {follower_addr[:8]} {symbol} 숏 포지션 추가: "
                        f"size {_old_size}→{_new_size}, avg_entry={_avg_entry:.4f}"
                    )
                    try:
                        await upsert_follower_position(
                            self.db, follower_addr, pos_key, "ask", _avg_entry, _new_size
                        )
                    except Exception as _e:
                        logger.debug(f"[PnL] 포지션 업데이트 오류 (무시): {_e}")
                else:
                    # 숏 포지션 신규 진입 — stop/take 계산
                    from core.strategy import _calc_stop_from_preset
                    sl_price, tp_price = _calc_stop_from_preset(exec_price, "ask", preset)
                    follower_positions[pos_key] = {
                        "entry_price": exec_price,
                        "size": float(copy_amount),
                        "side": "ask",
                        "high_price": exec_price,  # 숏에선 저점 추적
                        "stop_loss_price": sl_price or 0,
                        "take_profit_price": tp_price or 0,
                        "strategy": strategy_id,
                    }
                    try:
                        await upsert_follower_position(
                            self.db, follower_addr, pos_key, "ask", exec_price, float(copy_amount),
                            stop_loss_price=sl_price or 0,
                            take_profit_price=tp_price or 0,
                            high_price=exec_price,
                            strategy=strategy_id,
                        )
                        await self.db.execute(
                            "UPDATE positions SET high_price=?, stop_loss_price=?, take_profit_price=?, strategy=? "
                            "WHERE follower_address=? AND symbol=? AND status='open'",
                            (exec_price, sl_price or 0, tp_price or 0, strategy_id, follower_addr, pos_key)
                        )
                        await self.db.commit()
                        if sl_price:
                            logger.info(f"[SL] {follower_addr[:8]} {symbol} 숏 SL=${sl_price:.4f}")
                    except Exception as _e:
                        logger.debug(f"[PnL] 포지션 저장 오류 (무시): {_e}")

        # ── copy_trades 기록 ──────────────────────────────────
        _now_ms = int(time.time() * 1000)

        # 현재 포지션 진입가 기록용
        _entry = None
        _fp = self._positions.get(follower_addr, {})
        if symbol in _fp:
            _entry = _fp[symbol].get("entry_price")

        # P1 Fix (Round 6): fee_usdc를 copy_trades에 함께 기록
        _trade_fee_usdc = 0.0
        if status == "filled" and exec_price > 0:
            try:
                _trade_fee_usdc = round(float(copy_amount) * exec_price * float(BUILDER_FEE_RATE), 6)
            except Exception:
                pass

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
            "created_at": _now_ms,
            "error_msg": _error_msg if status == "failed" else None,
            "fee_usdc": _trade_fee_usdc,
        })


    async def force_close_follower(
        self,
        follower_addr: str,
        symbol: str,
        close_side: str,
        size: float,
        current_price: float,
        reason: str,
    ) -> None:
        """StopLossMonitor 호출 — 특정 팔로워 포지션 강제 청산"""
        # followers 테이블에서 팔로워 정보 조회
        async with self.db.execute(
            "SELECT * FROM followers WHERE address=?", (follower_addr,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                logger.warning(f"force_close: 팔로워 없음 {follower_addr[:16]}")
                return
            cols = [d[0] for d in cur.description]
            follower = dict(zip(cols, row))

        trader_address = follower.get("trader_address", "")
        copy_amount    = str(size)
        trade_id       = f"sl-{symbol}-{int(time.time() * 1000)}"
        client_order_id = trade_id

        logger.info(
            f"[FORCE_CLOSE] {follower_addr[:8]} {symbol} {close_side} "
            f"size={size} price={current_price:.4f} | {reason}"
        )

        if not self.mock_mode:
            try:
                bc = follower.get("builder_code_approved", 0)
                builder = BUILDER_CODE if bc else None
                client = self._get_client(follower_addr)
                resp = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: client.market_order(
                        symbol=symbol,
                        side=close_side,
                        amount=copy_amount,
                        slippage_percent=MAX_SLIPPAGE,
                        builder_code=builder,
                        client_order_id=client_order_id,
                    )
                )
                status = "filled" if resp.get("data") else "failed"
            except Exception as e:
                status = "failed"
                logger.error(f"[FORCE_CLOSE] 주문 오류: {e}")
        else:
            status = "filled"

        # ── P0 Fix (Round 5): force_close PnL 계산 ──────────────────────────
        # 포지션 진입가에서 현재가 기준으로 realized PnL 계산
        _force_realized_pnl = None
        _force_entry_price = None
        follower_positions = self._positions.get(follower_addr, {})
        if symbol in follower_positions:
            _fp = follower_positions[symbol]
            _force_entry_price = _fp.get("entry_price")
            if _force_entry_price and current_price > 0:
                _fp_side = _fp.get("side", "bid")
                if _fp_side == "bid":
                    # 롱 포지션 청산: (청산가 - 진입가) × size
                    _force_realized_pnl = round((current_price - _force_entry_price) * float(copy_amount), 6)
                else:
                    # 숏 포지션 청산: (진입가 - 청산가) × size
                    _force_realized_pnl = round((_force_entry_price - current_price) * float(copy_amount), 6)
                logger.info(
                    f"[FORCE_CLOSE][PnL] {follower_addr[:8]} {symbol} "
                    f"entry={_force_entry_price:.4f} close={current_price:.4f} "
                    f"pnl={_force_realized_pnl:+.6f}"
                )
        else:
            # DB에서 포지션 조회 시도
            try:
                _db_pos = await get_follower_position(self.db, follower_addr, symbol)
                if _db_pos and _db_pos.get("entry_price") and current_price > 0:
                    _force_entry_price = _db_pos["entry_price"]
                    _db_side = _db_pos.get("side", "bid")
                    if _db_side == "bid":
                        _force_realized_pnl = round((current_price - _force_entry_price) * float(copy_amount), 6)
                    else:
                        _force_realized_pnl = round((_force_entry_price - current_price) * float(copy_amount), 6)
            except Exception as _ep:
                logger.debug(f"[FORCE_CLOSE] PnL DB 조회 오류 (무시): {_ep}")

        # 포지션 삭제
        try:
            await delete_follower_position(self.db, follower_addr, symbol)
        except Exception:
            pass
        follower_positions.pop(symbol, None)

        # 기록
        _now_ms = int(time.time() * 1000)
        await record_copy_trade(self.db, {
            "id": trade_id,
            "follower_address": follower_addr,
            "trader_address": trader_address,
            "symbol": symbol,
            "side": close_side,
            "amount": copy_amount,
            "price": str(current_price),
            "client_order_id": client_order_id,
            "status": status,
            "pnl": _force_realized_pnl,
            "entry_price": _force_entry_price,
            "exec_price": current_price,
            "created_at": _now_ms,
            "error_msg": reason if status == "failed" else None,
        })
        logger.info(f"[FORCE_CLOSE] 완료: {follower_addr[:8]} {symbol} status={status}")


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
