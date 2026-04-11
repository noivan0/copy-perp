"""
Stop Loss Monitor — 손절/익절/트레일링 스탑 실시간 모니터링
────────────────────────────────────────────────────────────
- 30초마다 열린 포지션 전체 스캔
- 전략 프리셋에 따라 SL/TP/트레일링 조건 확인
- 조건 충족 시 → 즉시 시장가 청산 주문 실행
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.parse
import urllib3

import aiosqlite
import requests

from core.strategy import _should_stop_from_preset, STRATEGY_PASSIVE
from core.strategy_presets import get_preset

urllib3.disable_warnings()
logger = logging.getLogger(__name__)

PROXY = "https://api.codetabs.com/v1/proxy/?quest="
BASE  = "https://api.pacifica.fi/api/v1"
SCAN_INTERVAL = 30   # 30초마다 스캔


def _get_mark_prices() -> dict[str, float]:
    """현재 마크 가격 일괄 조회 (codetabs 프록시 → /info/prices)"""
    url = BASE + "/info/prices"
    try:
        r = requests.get(PROXY + urllib.parse.quote(url), timeout=15)
        if r.ok:
            raw = r.json()
            # /info/prices 응답: list 또는 {data: list}
            data = raw if isinstance(raw, list) else (raw.get("data", []) or [])
            return {
                m.get("symbol", "").upper(): float(m.get("mark_price", m.get("mark", 0)) or 0)
                for m in data
            }
    except Exception as e:
        logger.warning(f"마크 가격 조회 오류: {e}")
    return {}


class StopLossMonitor:
    """
    열린 포지션 실시간 손절/익절/트레일링 모니터
    CopyEngine의 on_fill 콜백을 재사용하여 청산 주문 실행
    """

    def __init__(self, db: aiosqlite.Connection, copy_engine):
        self.db = db
        self.engine = copy_engine   # CopyEngine 인스턴스 (청산 주문용)
        self._running = False

    async def start(self):
        self._running = True
        logger.info("StopLossMonitor 시작")
        while self._running:
            try:
                await self._scan_positions()
            except Exception as e:
                logger.error(f"StopLossMonitor 오류: {e}")
            await asyncio.sleep(SCAN_INTERVAL)

    async def stop(self):
        self._running = False

    async def _scan_positions(self):
        """DB의 열린 포지션 전체 스캔 → 손절 조건 확인
        
        P0 Fix (Round 6): positions 테이블과 follower_positions 테이블 이중 스캔
        - CopyEngine은 follower_positions에 저장 (기본)
        - pnl_tracker는 positions 테이블 사용 (별도)
        - 두 테이블 UNION하여 누락 없이 스캔
        """
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
            logger.debug(f"[StopLoss] positions 테이블 조회 오류 (무시): {_e}")

        # 2차: follower_positions 테이블 (CopyEngine 관리) — positions에 없는 항목만 추가
        try:
            existing_keys = {(r["follower_address"], r["symbol"]) for r in rows}
            async with self.db.execute("""
                SELECT fp.follower_address, fp.symbol, fp.side,
                       fp.entry_price AS avg_entry_price, fp.size,
                       0 AS stop_loss_price,
                       0 AS take_profit_price,
                       fp.entry_price AS high_price,
                       COALESCE(f.strategy, 'passive') AS strategy,
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
            logger.debug(f"[StopLoss] follower_positions 테이블 조회 오류 (무시): {_e}")

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
                    await self.db.execute(
                        "UPDATE positions SET high_price=? WHERE follower_address=? AND symbol=? AND status='open'",
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
        """강제 청산 — 반대 방향 시장가 주문"""
        follower_addr = pos["follower_address"]
        symbol        = pos["symbol"]
        side          = pos["side"]
        size          = pos["size"]
        reason        = pos["reason"]
        strategy      = pos["strategy"]

        # 반대 방향
        close_side = "ask" if side == "bid" else "bid"

        logger.warning(
            f"[{strategy.upper()}] {follower_addr[:8]} {symbol} 강제청산 | "
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
            "_force_follower": follower_addr,   # 특정 팔로워만 강제 청산
        }

        try:
            await self.engine.force_close_follower(follower_addr, symbol, close_side, size, pos["current"], reason)
        except AttributeError:
            # force_close_follower 없으면 on_fill 폴백
            await self.engine.on_fill(fake_event)
        except Exception as e:
            logger.error(f"강제청산 오류 {follower_addr[:8]} {symbol}: {e}")
