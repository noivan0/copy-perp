"""
4가지 전략 동시 페이퍼트레이딩 시스템
────────────────────────────────────────────────────────────────
- default / conservative / balanced / aggressive 4개를 동시에 실행
- mainnet 트레이더 포지션 변화를 실시간 감지
- 각 전략의 copy_ratio / stop_loss / take_profit 독립 적용
- DB(paper_sessions, paper_trades)에 누적 기록
- 15분마다 스냅샷 → 시간이 쌓일수록 전략 비교 가능
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import time
import urllib.parse
import logging
import signal
from datetime import datetime, date
from typing import Optional

import requests
import urllib3

urllib3.disable_warnings()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.strategy_presets import PRESETS, get_preset

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("/tmp/pt4strategy.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("pt4")

PROXY = "https://api.codetabs.com/v1/proxy/?quest="
BASE  = "https://api.pacifica.fi/api/v1"
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "copy_perp.db")

POLL_INTERVAL  = 120  # 2분마다 포지션 폴링
SNAP_INTERVAL  = 900  # 15분마다 스냅샷

# 추적 트레이더 (mainnet CARP 상위, 2026-03-19 기준)
WATCH_TRADERS = [
    ("YjCD9Gek", "YjCD9Gek6MVY9t3MLEGYYdZLeaF6MZrpgZraayWsv9E"),
    ("GTU92nBC",  "GTU92nBC8LMyt9W4Qqc319BFR1vpkNNPAbt4QCnX7kZ6"),
    ("3iKDU1jU",  "3iKDU1jUU1KrJXFkYuQBRUALSFKbnWUFjx1o8E7VqxhG"),
    ("5RX2DD42",  "5RX2DD425DHjJHJWYSiJcFh7BsRb6b66UFYSmB2jJBHs"),
    ("4TYEjn9P",  "4TYEjn9PSpxoBNBXWgvUGaqQ8B4sNHRcLUEbA9mHzPfZ"),
]

INITIAL_CAPITAL = 10_000.0  # 전략별 시작 자본

# ── DB 초기화 ─────────────────────────────────────────────
def init_pt_tables(conn: sqlite3.Connection):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS paper_sessions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy        TEXT NOT NULL,
        started_at      INTEGER NOT NULL,
        last_updated    INTEGER NOT NULL,
        initial_capital REAL DEFAULT 10000,
        current_equity  REAL DEFAULT 10000,
        realized_pnl    REAL DEFAULT 0,
        unrealized_pnl  REAL DEFAULT 0,
        total_trades    INTEGER DEFAULT 0,
        win_trades      INTEGER DEFAULT 0,
        lose_trades     INTEGER DEFAULT 0,
        UNIQUE(strategy)
    );

    CREATE TABLE IF NOT EXISTS paper_trades (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        session_strategy TEXT NOT NULL,
        trader_alias    TEXT,
        symbol          TEXT NOT NULL,
        side            TEXT NOT NULL,
        action          TEXT NOT NULL,  -- 'open' / 'close' / 'stop_loss' / 'take_profit' / 'trailing'
        size            REAL NOT NULL,
        entry_price     REAL NOT NULL,
        exit_price      REAL DEFAULT 0,
        pnl             REAL DEFAULT 0,
        roi_pct         REAL DEFAULT 0,
        hold_min        REAL DEFAULT 0,
        opened_at       INTEGER NOT NULL,
        closed_at       INTEGER DEFAULT 0,
        stop_loss_price REAL DEFAULT 0,
        take_profit_price REAL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS paper_snapshots (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy        TEXT NOT NULL,
        snapshot_at     INTEGER NOT NULL,
        equity          REAL NOT NULL,
        realized_pnl    REAL DEFAULT 0,
        unrealized_pnl  REAL DEFAULT 0,
        open_positions  INTEGER DEFAULT 0,
        total_trades    INTEGER DEFAULT 0,
        win_rate        REAL DEFAULT 0,
        UNIQUE(strategy, snapshot_at)
    );

    CREATE INDEX IF NOT EXISTS idx_paper_trades_strategy ON paper_trades(session_strategy, opened_at DESC);
    CREATE INDEX IF NOT EXISTS idx_paper_snapshots_ts ON paper_snapshots(strategy, snapshot_at DESC);
    """)
    conn.commit()


# ── API ──────────────────────────────────────────────────
def pm_get(path: str, params: dict = None) -> dict | list | None:
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        r = requests.get(PROXY + urllib.parse.quote(url), timeout=20)
        return r.json() if r.ok else None
    except Exception as e:
        logger.debug(f"API 오류 {path}: {e}")
        return None

def get_trader_positions(address: str) -> list:
    """
    mainnet_trades DB에서 현재 오픈 포지션 추론
    open_long → bid, open_short → ask
    close_long → bid 청산, close_short → ask 청산
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("""
            SELECT symbol, side, amount, entry_price, created_at
            FROM mainnet_trades
            WHERE trader_address = ?
              AND event_type IN ('fulfill_taker', 'fulfill_maker')
            ORDER BY created_at ASC
        """, (address,))
        rows = cur.fetchall()
        conn.close()

        # 심볼별 net 포지션 계산
        positions: dict[str, dict] = {}
        for sym, side, amt, price, ts in rows:
            amt_f = float(amt or 0)
            price_f = float(price or 0)
            if amt_f <= 0:
                continue

            if side == "open_long":
                if sym not in positions:
                    positions[sym] = {"side": "bid", "size": 0.0, "entry_price": price_f}
                if positions[sym].get("side") == "bid":
                    # 가중평균 진입가
                    old = positions[sym]
                    new_size = old["size"] + amt_f
                    new_entry = (old["entry_price"] * old["size"] + price_f * amt_f) / new_size
                    positions[sym] = {"side": "bid", "size": new_size, "entry_price": new_entry}
                else:
                    # 방향 전환
                    positions[sym] = {"side": "bid", "size": amt_f, "entry_price": price_f}

            elif side == "open_short":
                if sym not in positions:
                    positions[sym] = {"side": "ask", "size": 0.0, "entry_price": price_f}
                if positions[sym].get("side") == "ask":
                    old = positions[sym]
                    new_size = old["size"] + amt_f
                    new_entry = (old["entry_price"] * old["size"] + price_f * amt_f) / new_size
                    positions[sym] = {"side": "ask", "size": new_size, "entry_price": new_entry}
                else:
                    positions[sym] = {"side": "ask", "size": amt_f, "entry_price": price_f}

            elif side in ("close_long", "close_short"):
                if sym in positions:
                    positions[sym]["size"] = max(0.0, positions[sym]["size"] - amt_f)
                    if positions[sym]["size"] < 1e-8:
                        del positions[sym]

        # 결과 리스트 변환
        result = []
        for sym, pos in positions.items():
            if pos["size"] > 1e-8:
                result.append({
                    "symbol": sym,
                    "side": pos["side"],
                    "size": pos["size"],
                    "entry_price": pos["entry_price"],
                })
        return result
    except Exception as e:
        logger.debug(f"포지션 추론 오류 {address}: {e}")
        return []

def get_mark_prices() -> dict[str, float]:
    """최근 mainnet_trades에서 마크가 추정"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("""
            SELECT symbol, entry_price FROM mainnet_trades
            WHERE created_at = (
                SELECT MAX(created_at) FROM mainnet_trades m2
                WHERE m2.symbol = mainnet_trades.symbol
            )
            AND entry_price > 0
        """)
        prices = {r[0]: float(r[1]) for r in cur.fetchall()}
        conn.close()
        return prices
    except Exception:
        return {}


# ── 전략 엔진 ─────────────────────────────────────────────
class StrategyEngine:
    """단일 전략 페이퍼트레이딩 엔진"""

    def __init__(self, strategy_id: str, conn: sqlite3.Connection):
        self.strategy_id = strategy_id
        self.preset = get_preset(strategy_id)
        self.conn = conn
        self.equity = INITIAL_CAPITAL
        self.realized_pnl = 0.0
        self.positions: dict[str, dict] = {}  # key: f"{trader}_{symbol}"
        self.now_ms = int(time.time() * 1000)
        self._init_session()

    def _init_session(self):
        cur = self.conn.cursor()
        cur.execute("""
            INSERT OR IGNORE INTO paper_sessions
            (strategy, started_at, last_updated, initial_capital, current_equity)
            VALUES (?, ?, ?, ?, ?)
        """, (self.strategy_id, self.now_ms, self.now_ms, INITIAL_CAPITAL, INITIAL_CAPITAL))
        self.conn.commit()

        # 기존 세션 복원
        cur.execute("SELECT current_equity, realized_pnl FROM paper_sessions WHERE strategy=?", (self.strategy_id,))
        row = cur.fetchone()
        if row:
            self.equity = float(row[0])
            self.realized_pnl = float(row[1])

        # 열린 포지션 복원
        cur.execute("""
            SELECT trader_alias, symbol, side, size, entry_price,
                   stop_loss_price, take_profit_price, opened_at
            FROM paper_trades
            WHERE session_strategy=? AND closed_at=0
        """, (self.strategy_id,))
        for r in cur.fetchall():
            key = f"{r[0]}_{r[1]}"
            self.positions[key] = {
                "trader": r[0], "symbol": r[1], "side": r[2],
                "size": float(r[3]), "entry": float(r[4]),
                "sl": float(r[5]), "tp": float(r[6]),
                "high": float(r[4]),  # 트레일링용 고점
                "opened_at": int(r[7]),
            }

        open_cnt = len(self.positions)
        logger.info(f"[{self.strategy_id}] 세션 복원: equity=${self.equity:.2f} pnl={self.realized_pnl:+.4f} positions={open_cnt}")

    def on_trader_positions(self, trader_alias: str, trader_addr: str, positions: list, prices: dict):
        """트레이더 포지션 업데이트 처리"""
        now_ms = int(time.time() * 1000)
        copy_ratio = self.preset.get("copy_ratio", 0.10)
        sl_pct  = self.preset.get("stop_loss_pct", 0.0)
        tp_pct  = self.preset.get("take_profit_pct", 0.0)
        tr_pct  = self.preset.get("trailing_stop_pct", 0.0)
        max_pos = self.preset.get("max_position_usdc", 120.0)

        # 현재 트레이더의 활성 심볼 집합
        active_symbols = set()
        for pos in positions:
            sym  = pos.get("symbol", "")
            side = pos.get("side", "")
            size = float(pos.get("size", 0) or 0)
            entry = float(pos.get("entry_price", 0) or 0)
            if size <= 0 or entry <= 0:
                continue

            active_symbols.add(sym)
            key = f"{trader_alias}_{sym}"

            if key not in self.positions:
                # 신규 포지션 진입
                copy_size = size * copy_ratio
                notional  = copy_size * entry
                if notional > max_pos:
                    copy_size = max_pos / entry

                sl_price = tp_price = 0.0
                if side == "bid" and sl_pct > 0:
                    sl_price = entry * (1 - sl_pct)
                elif side == "ask" and sl_pct > 0:
                    sl_price = entry * (1 + sl_pct)

                if side == "bid" and tp_pct > 0:
                    tp_price = entry * (1 + tp_pct)
                elif side == "ask" and tp_pct > 0:
                    tp_price = entry * (1 - tp_pct)

                self.positions[key] = {
                    "trader": trader_alias, "symbol": sym, "side": side,
                    "size": copy_size, "entry": entry,
                    "sl": sl_price, "tp": tp_price, "high": entry,
                    "opened_at": now_ms,
                }
                self._record_open(trader_alias, sym, side, copy_size, entry, sl_price, tp_price, now_ms)
                logger.info(f"[{self.strategy_id}] OPEN {trader_alias} {sym} {side} "
                            f"size={copy_size:.4f} entry={entry:.4f} SL={sl_price:.4f}")
            else:
                # 기존 포지션 — 고점 갱신 + 손절 체크
                pos_info = self.positions[key]
                cur_price = prices.get(sym, entry)

                # 트레일링: 고점 갱신
                if side == "bid" and cur_price > pos_info["high"]:
                    self.positions[key]["high"] = cur_price
                elif side == "ask" and cur_price < pos_info["high"]:
                    self.positions[key]["high"] = cur_price

                close_reason = self._check_stop(pos_info, cur_price)
                if close_reason:
                    pnl = self._close_position(key, cur_price, now_ms, close_reason)
                    logger.warning(f"[{self.strategy_id}] {close_reason} {trader_alias} {sym} pnl={pnl:+.4f}")
                    continue

        # 트레이더가 청산한 포지션 → 팔로워도 청산
        to_close = [k for k in self.positions
                    if k.startswith(f"{trader_alias}_")
                    and self.positions[k]["symbol"] not in active_symbols]
        for key in to_close:
            sym = self.positions[key]["symbol"]
            cur_price = prices.get(sym, self.positions[key]["entry"])
            pnl = self._close_position(key, cur_price, now_ms, "TRADER_CLOSE")
            logger.info(f"[{self.strategy_id}] CLOSE {key} pnl={pnl:+.4f}")

    def _check_stop(self, pos: dict, cur_price: float) -> str:
        """손절/익절/트레일링 조건 확인"""
        entry = pos["entry"]
        side  = pos["side"]
        sl_pct = self.preset.get("stop_loss_pct", 0.0)
        tp_pct = self.preset.get("take_profit_pct", 0.0)
        tr_pct = self.preset.get("trailing_stop_pct", 0.0)

        if entry <= 0 or cur_price <= 0:
            return ""

        roi = (cur_price - entry) / entry if side == "bid" else (entry - cur_price) / entry

        if sl_pct > 0 and roi <= -sl_pct:
            return f"STOP_LOSS({roi*100:.1f}%)"
        if tp_pct > 0 and roi >= tp_pct:
            return f"TAKE_PROFIT({roi*100:.1f}%)"
        if tr_pct > 0 and pos.get("high", entry) > 0:
            high = pos["high"]
            if side == "bid":
                dd = (cur_price - high) / high
            else:
                dd = (high - cur_price) / high
            if dd <= -tr_pct:
                return f"TRAILING({dd*100:.1f}%)"
        return ""

    def _close_position(self, key: str, exit_price: float, now_ms: int, reason: str) -> float:
        if key not in self.positions:
            return 0.0
        pos = self.positions.pop(key)
        entry = pos["entry"]
        size  = pos["size"]
        side  = pos["side"]

        if side == "bid":
            pnl = (exit_price - entry) * size
        else:
            pnl = (entry - exit_price) * size

        roi_pct = pnl / (entry * size) * 100 if entry * size > 0 else 0
        hold_min = (now_ms - pos["opened_at"]) / 60000

        self.realized_pnl += pnl
        self.equity += pnl

        cur = self.conn.cursor()
        cur.execute("""
            UPDATE paper_trades
            SET exit_price=?, pnl=?, roi_pct=?, hold_min=?, closed_at=?, action=?
            WHERE session_strategy=? AND trader_alias=? AND symbol=?
              AND closed_at=0
        """, (exit_price, pnl, roi_pct, hold_min, now_ms, reason,
              self.strategy_id, pos["trader"], pos["symbol"]))

        is_win = pnl > 0
        cur.execute("""
            UPDATE paper_sessions
            SET current_equity=?, realized_pnl=?,
                total_trades=total_trades+1,
                win_trades=win_trades+?,
                lose_trades=lose_trades+?,
                last_updated=?
            WHERE strategy=?
        """, (self.equity, self.realized_pnl,
              1 if is_win else 0,
              0 if is_win else 1,
              now_ms, self.strategy_id))
        self.conn.commit()
        return pnl

    def _record_open(self, trader: str, symbol: str, side: str, size: float,
                     entry: float, sl: float, tp: float, opened_at: int):
        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO paper_trades
            (session_strategy, trader_alias, symbol, side, action, size,
             entry_price, stop_loss_price, take_profit_price, opened_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (self.strategy_id, trader, symbol, side, "open",
              size, entry, sl, tp, opened_at))
        self.conn.commit()

    def save_snapshot(self):
        now_ms = int(time.time() * 1000)
        snap_key = now_ms // (SNAP_INTERVAL * 1000) * (SNAP_INTERVAL * 1000)  # 15분 버킷

        unrealized = self._calc_unrealized()

        cur = self.conn.cursor()
        cur.execute("SELECT total_trades, win_trades FROM paper_sessions WHERE strategy=?", (self.strategy_id,))
        row = cur.fetchone()
        total = int(row[0]) if row else 0
        wins  = int(row[1]) if row else 0
        wr = wins / total * 100 if total > 0 else 0

        try:
            cur.execute("""
                INSERT OR IGNORE INTO paper_snapshots
                (strategy, snapshot_at, equity, realized_pnl, unrealized_pnl,
                 open_positions, total_trades, win_rate)
                VALUES (?,?,?,?,?,?,?,?)
            """, (self.strategy_id, snap_key, self.equity,
                  self.realized_pnl, unrealized, len(self.positions), total, wr))
            self.conn.commit()
        except Exception:
            pass

    def _calc_unrealized(self) -> float:
        return 0.0  # 현재가 실시간 조회 없이 0으로 처리 (DB에 최근가 없을 때)

    def summary(self) -> dict:
        cur = self.conn.cursor()
        cur.execute("""
            SELECT total_trades, win_trades, lose_trades
            FROM paper_sessions WHERE strategy=?
        """, (self.strategy_id,))
        row = cur.fetchone()
        total = int(row[0]) if row else 0
        wins  = int(row[1]) if row else 0
        wr    = wins / total * 100 if total > 0 else 0
        roi   = (self.equity - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100

        return {
            "strategy":      self.strategy_id,
            "label":         self.preset.get("label", self.strategy_id),
            "emoji":         self.preset.get("emoji", ""),
            "equity":        round(self.equity, 4),
            "realized_pnl":  round(self.realized_pnl, 4),
            "roi_pct":       round(roi, 4),
            "open_positions":len(self.positions),
            "total_trades":  total,
            "win_rate":      round(wr, 1),
        }


# ── 메인 루프 ─────────────────────────────────────────────
def print_dashboard(engines: list[StrategyEngine], poll: int):
    SEP = "=" * 72
    now = datetime.now().strftime("%Y-%m-%d %H:%M KST")
    print(f"\n{SEP}")
    print(f"  4전략 페이퍼트레이딩 | {now} | poll #{poll}")
    print(SEP)
    print(f"  {'전략':<14} {'자본':>10} {'실현PnL':>10} {'ROI':>7} {'포지션':>6} {'거래':>5} {'WR':>6}")
    print(f"  {'-'*68}")
    for e in engines:
        s = e.summary()
        roi_s = f"{s['roi_pct']:+.2f}%"
        pnl_s = f"${s['realized_pnl']:+.4f}"
        star  = "✅" if s['realized_pnl'] > 0 else ("🔴" if s['realized_pnl'] < 0 else "⏳")
        print(f"  {star} {s['emoji']} {s['label']:<11} ${s['equity']:>9,.2f} {pnl_s:>10} {roi_s:>7} {s['open_positions']:>6} {s['total_trades']:>5} {s['win_rate']:>5.1f}%")
    print(SEP)


def run(loop: bool = True):
    conn = sqlite3.connect(DB_PATH)
    init_pt_tables(conn)

    # 4전략 엔진 초기화
    strategies = ["default", "conservative", "balanced", "aggressive"]
    engines = [StrategyEngine(sid, conn) for sid in strategies]

    logger.info(f"4전략 페이퍼트레이딩 시작 | 자본 ${INITIAL_CAPITAL:,.0f}/전략 | 폴링 {POLL_INTERVAL}초")

    poll = 0
    last_snap = time.time()
    running = True

    def _stop(sig, frame):
        nonlocal running
        logger.info("종료 신호 수신")
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT,  _stop)

    while running:
        poll += 1
        prices = get_mark_prices()

        for alias, addr in WATCH_TRADERS:
            positions = get_trader_positions(addr)
            time.sleep(0.3)  # rate limit
            for engine in engines:
                try:
                    engine.on_trader_positions(alias, addr, positions, prices)
                except Exception as e:
                    logger.error(f"[{engine.strategy_id}] 처리 오류: {e}")

        # 스냅샷
        if time.time() - last_snap >= SNAP_INTERVAL:
            for engine in engines:
                engine.save_snapshot()
            last_snap = time.time()

        print_dashboard(engines, poll)

        if not loop:
            break

        # 다음 폴링까지 대기
        time.sleep(POLL_INTERVAL)

    conn.close()
    logger.info("4전략 페이퍼트레이딩 종료")


if __name__ == "__main__":
    import sys
    loop_mode = "--once" not in sys.argv
    run(loop=loop_mode)
