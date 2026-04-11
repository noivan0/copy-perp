"""
Stop Loss Monitor — real-time SL/TP/Trailing stop
──────────────────────────────────────────────────
- Scans all open positions every 30 seconds
- Checks SL/TP/trailing conditions per strategy preset
- Executes market close order when condition is met
"""

from __future__ import annotations

import asyncio
import logging
import time

from core.strategy import _should_stop_from_preset, STRATEGY_PASSIVE
from core.strategy_presets import get_preset

logger = logging.getLogger(__name__)

SCAN_INTERVAL = 30   # scan every 30 seconds


def _get_mark_prices() -> dict[str, float]:
    """R10: DataCollector price_cache 재사용 (외부 HTTP 제거)
    - 외부 codetabs proxy 의존성 제거 → 신뢰도/latency 개선
    - data_collector.get_mark_price() 는 per-symbol TTL 체크 포함
    - fallback: price_cache 전체 스캔
    """
    try:
        from core.data_collector import get_price_cache
        cache = get_price_cache()
        result: dict[str, float] = {}
        for sym, entry in cache.items():
            try:
                mark = float(entry.get("mark", 0) or 0)
                if mark > 0:
                    result[sym.upper()] = mark
            except (TypeError, ValueError):
                pass
        if result:
            return result
    except Exception as e:
        logger.debug(f"[StopLoss] price_cache 조회 오류: {e}")
    return {}


class StopLossMonitor:
    """
    Real-time SL/TP/trailing stop monitor
    Reuses CopyEngine.on_fill callback for close orders
    """

    def __init__(self, db, copy_engine):  # db: TursoDb or aiosqlite.Connection
        self.db = db
        self.engine = copy_engine   # CopyEngine instance (used for close orders)
        self._running = False

    async def start(self):
        self._running = True
        logger.info("StopLossMonitor started")
        while self._running:
            try:
                await self._scan_positions()
            except Exception as e:
                logger.error(f"StopLossMonitor error: {e}")
            await asyncio.sleep(SCAN_INTERVAL)

    async def stop(self):
        self._running = False

    async def _scan_positions(self):
        """Scan all open positions in DB → check SL/TP conditions
        
        P0 Fix (Round 6): dual scan of positions and follower_positions tables
        - CopyEngine stores in follower_positions (primary)
        - pnl_tracker uses positions table (secondary)
        - UNION both tables for complete coverage
        R13 P2: 포지션 없을 때 가격 조회 및 DB 쿼리 스킵 (early exit)
        """
        # ── R13 P2: 활성 팔로워 없으면 즉시 종료 (불필요한 DB 쿼리 방지) ──
        try:
            async with self.db.execute(
                "SELECT COUNT(*) FROM followers WHERE active != 0"
            ) as _cnt_cur:
                _active_followers = (await _cnt_cur.fetchone())[0] or 0
            if _active_followers == 0:
                logger.debug("[StopLoss] 활성 팔로워 없음 — 스캔 스킵")
                return
        except Exception as _ef:
            logger.debug(f"[StopLoss] 팔로워 카운트 오류 (무시): {_ef}")

        # 현재가 조회
        prices = _get_mark_prices()
        if not prices:
            return

        rows = []

        # 1차: positions 테이블 (pnl_tracker 관리)
        try:
            async with self.db.execute("""
                SELECT p.follower_address, p.symbol, p.side,
                       p.avg_entry_price AS avg_entry_price, p.size,
                       COALESCE(p.stop_loss_price, 0) AS stop_loss_price,
                       COALESCE(p.take_profit_price, 0) AS take_profit_price,
                       COALESCE(p.high_price, p.avg_entry_price) AS high_price,
                       COALESCE(p.strategy, 'passive') AS strategy,
                       COALESCE(f.strategy, 'passive') AS follower_strategy,
                       COALESCE(p.trader_address, '') AS trader_address
                FROM positions p
                JOIN followers f ON p.follower_address = f.address
                WHERE p.status = 'open'
                  AND p.avg_entry_price > 0
            """) as cur:
                rows += [dict(zip([d[0] for d in cur.description], r)) for r in await cur.fetchall()]
        except Exception as _e:
            logger.debug(f"[StopLoss] positions table query error (ignored): {_e}")

        # 2차: follower_positions 테이블 (CopyEngine 관리) — positions에 없는 항목만 추가
        # R10: stop_loss_price/take_profit_price/high_price/strategy 실제 값 사용
        try:
            existing_keys = {(r["follower_address"], r["symbol"]) for r in rows}
            async with self.db.execute("""
                SELECT fp.follower_address, fp.symbol, fp.side,
                       fp.entry_price AS avg_entry_price, fp.size,
                       COALESCE(fp.stop_loss_price, 0) AS stop_loss_price,
                       COALESCE(fp.take_profit_price, 0) AS take_profit_price,
                       COALESCE(fp.high_price, fp.entry_price) AS high_price,
                       COALESCE(fp.strategy, f.strategy, 'passive') AS strategy,
                       COALESCE(f.strategy, 'passive') AS follower_strategy,
                       COALESCE(f.trader_address, '') AS trader_address
                FROM follower_positions fp
                JOIN followers f ON fp.follower_address = f.address
            """) as cur:
                for r in await cur.fetchall():
                    row_dict = dict(zip([d[0] for d in cur.description], r))
                    key = (row_dict["follower_address"], row_dict["symbol"])
                    if key not in existing_keys:
                        rows.append(row_dict)
        except Exception as _e:
            logger.debug(f"[StopLoss] follower_positions table query error (ignored): {_e}")

        if not rows:
            return

        triggered = []
        for pos in rows:
            sym          = pos["symbol"]
            current_price = prices.get(sym.upper(), 0)
            if current_price <= 0:
                continue

            entry  = float(pos["avg_entry_price"] or 0)
            side   = pos["side"]   # bid = long, ask = short
            high_p = float(pos["high_price"] or entry)
            strategy_id = pos["strategy"] or pos["follower_strategy"] or STRATEGY_PASSIVE
            preset = get_preset(strategy_id)

            # 트레일링 스탑: 고점(롱) / 저점(숏) 갱신
            new_high = high_p
            if side == "bid" and current_price > high_p:
                new_high = current_price
            elif side == "ask" and current_price < high_p:
                new_high = current_price

            if new_high != high_p:
                try:
                    # positions 테이블 업데이트
                    await self.db.execute(
                        "UPDATE positions SET high_price=? WHERE follower_address=? AND symbol=? AND status='open'",
                        (new_high, pos["follower_address"], sym)
                    )
                    # R10b: follower_positions 테이블도 동기화 (트레일링 스탑 정확도)
                    await self.db.execute(
                        "UPDATE follower_positions SET high_price=? WHERE follower_address=? AND symbol=?",
                        (new_high, pos["follower_address"], sym)
                    )
                    await self.db.commit()
                except Exception:
                    pass
                high_p = new_high

            # 손절/익절/트레일링 조건 체크
            stop, reason = _should_stop_from_preset(entry, current_price, high_p, side, preset)
            if stop:
                triggered.append({
                    "follower_address": pos["follower_address"],
                    "symbol": sym,
                    "side": side,
                    "size": float(pos["size"] or 0),
                    "entry": entry,
                    "current": current_price,
                    "reason": reason,
                    "trader_address": pos.get("trader_address", ""),
                    "strategy": strategy_id,
                })

        # 청산 실행
        for t in triggered:
            await self._force_close(t)

    async def _force_close(self, pos: dict):
        """Force close — reverse direction market order"""
        follower_addr = pos["follower_address"]
        symbol        = pos["symbol"]
        side          = pos["side"]
        size          = pos["size"]
        reason        = pos["reason"]
        strategy      = pos["strategy"]

        # 반대 방향
        close_side = "ask" if side == "bid" else "bid"

        logger.warning(
            f"[{strategy.upper()}] {follower_addr[:8]} {symbol} FORCE_CLOSE | "
            f"entry={pos['entry']:.4f} current={pos['current']:.4f} | {reason}"
        )

        # CopyEngine에 가상 fill 이벤트 주입 → 기존 청산 로직 재사용
        fake_event = {
            "event_type":     "stop_close",
            "symbol":         symbol,
            "side":           close_side,
            "amount":         str(size),
            "price":          str(pos["current"]),
            "account":        pos.get("trader_address", ""),
            "cause":          f"stop_loss:{reason}",
            "created_at":     int(time.time() * 1000),
            "_force_follower": follower_addr,   # target specific follower for force close
        }

        try:
            await self.engine.force_close_follower(follower_addr, symbol, close_side, size, pos["current"], reason)
        except AttributeError:
            # force_close_follower 없으면 on_fill 폴백
            await self.engine.on_fill(fake_event)
        except Exception as e:
            logger.error(f"Force close error {follower_addr[:8]} {symbol}: {e}")
