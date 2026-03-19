"""
Copy Perp — 4전략 통합 페이퍼트레이딩 엔진 v3
═══════════════════════════════════════════════════════════════════════
핵심 수정 (v3):
  - 포지션 키: alias_sym_direction (롱/숏 독립 추적)
  - 트레이더가 same-symbol 롱+숏 동시 보유 → 개별 처리
  - DB 분리: pt_paper.db (쓰기) / copy_perp.db (읽기 전용)
  - last_ts: 최초 실행 시 mainnet_trades 최대값으로 초기화 (이중처리 방지)
  - 로그 중복 제거

플로우:
  ① 30초마다 mainnet /trades/history 신규 거래 수집
  ② 수집 즉시 open/close 이벤트 파싱 → direction별 독립 포지션 delta
  ③ 4전략 동시 반영 (copy_ratio / SL / TP / trailing 독립 적용)
  ④ 청산 발생 시 즉시 PnL 기록 → DB 누적
  ⑤ 5분마다 스냅샷 저장
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import signal
import time
import urllib.parse
import logging
from datetime import datetime
from typing import Optional

import requests
import urllib3

urllib3.disable_warnings()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.strategy_presets import PRESETS, get_preset

# ── 설정 ─────────────────────────────────────────────────
PROXY          = "https://api.codetabs.com/v1/proxy/?quest="
BASE           = "https://api.pacifica.fi/api/v1"
_ROOT          = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH        = os.path.join(_ROOT, "copy_perp.db")
PT_DB_PATH     = os.path.join(_ROOT, "pt_paper.db")
POLL_SEC       = 30
SNAP_SEC       = 300
INITIAL_CAP    = 10_000.0
STRATEGIES     = ["default", "conservative", "balanced", "aggressive"]

TRADERS = [
    ("YjCD9Gek", "YjCD9Gek6MVY9t3MLEGYYdZLeaF6MZrpgZraayWsv9E"),
    ("GTU92nBC",  "GTU92nBC8LMyt9W4Qqc319BFR1vpkNNPAbt4QCnX7kZ6"),
    ("3iKDU1jU",  "3iKDU1jUU1KrJXFkYuQBRUALSFKbnWUFjx1o8E7VqxhG"),
    ("5RX2DD42",  "5RX2DD425DHjJHJWYSiJcFh7BsRb6b66UFYSmB2jJBHs"),
    ("4TYEjn9P",  "4TYEjn9PSpxoBNBXWgvUGaqQ8B4sNHRcLUEbA9mHzPfZ"),
    ("HtC4WT6J",  "HtC4WT6JhKz8eojNbkpAv16j5mB6JBj3y8EVbuVzHkCZ"),
]

# 로그 설정 (중복 방지)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
_fh  = logging.FileHandler("/tmp/pt_engine.log")
_fh.setFormatter(_fmt)
_sh  = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
logging.root.setLevel(logging.INFO)
logging.root.handlers = [_fh, _sh]
# propagation 차단 (중복 방지)
logging.root.propagate = False
log = logging.getLogger("pt")

# ── DB 스키마 ─────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS pt_sessions (
    strategy        TEXT PRIMARY KEY,
    started_at      INTEGER NOT NULL,
    last_updated    INTEGER NOT NULL,
    initial_capital REAL    DEFAULT 10000,
    equity          REAL    DEFAULT 10000,
    realized_pnl    REAL    DEFAULT 0,
    gross_profit    REAL    DEFAULT 0,
    gross_loss      REAL    DEFAULT 0,
    total_trades    INTEGER DEFAULT 0,
    win_trades      INTEGER DEFAULT 0,
    max_drawdown    REAL    DEFAULT 0,
    peak_equity     REAL    DEFAULT 10000
);
CREATE TABLE IF NOT EXISTS pt_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy        TEXT    NOT NULL,
    trader_alias    TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    direction       TEXT    NOT NULL,
    size            REAL    NOT NULL,
    entry_price     REAL    NOT NULL,
    exit_price      REAL    DEFAULT 0,
    gross_pnl       REAL    DEFAULT 0,
    net_pnl         REAL    DEFAULT 0,
    fee             REAL    DEFAULT 0,
    roi_pct         REAL    DEFAULT 0,
    hold_min        REAL    DEFAULT 0,
    opened_at       INTEGER NOT NULL,
    closed_at       INTEGER DEFAULT 0,
    close_reason    TEXT    DEFAULT '',
    stop_loss_price REAL    DEFAULT 0,
    take_profit_price REAL  DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pt_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy        TEXT    NOT NULL,
    snap_at         INTEGER NOT NULL,
    equity          REAL    NOT NULL,
    realized_pnl    REAL    DEFAULT 0,
    open_positions  INTEGER DEFAULT 0,
    total_trades    INTEGER DEFAULT 0,
    win_rate        REAL    DEFAULT 0,
    profit_factor   REAL    DEFAULT 0,
    max_drawdown    REAL    DEFAULT 0,
    roi_pct         REAL    DEFAULT 0,
    UNIQUE(strategy, snap_at)
);
CREATE TABLE IF NOT EXISTS pt_last_ts (
    trader_alias    TEXT    PRIMARY KEY,
    last_ts         INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pt_trades_strategy ON pt_trades(strategy, closed_at DESC);
"""

FEE_RATE = 0.0015  # taker + builder fee


# ══════════════════════════════════════════════════════════
# API
# ══════════════════════════════════════════════════════════
def api_get(path: str, params: dict = None) -> Optional[dict | list]:
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        r = requests.get(PROXY + urllib.parse.quote(url), timeout=20)
        return r.json() if r.ok else None
    except Exception as e:
        log.debug(f"API 오류 {path}: {e}")
        return None


def fetch_new_trades(alias: str, address: str, last_ts: int) -> list[dict]:
    d = api_get("/trades/history", {"address": address, "limit": 500})
    if not d:
        return []
    trades = (d.get("data") or []) if isinstance(d, dict) else (d or [])
    new_trades = [
        t for t in trades
        if t.get("created_at", 0) > last_ts
        and t.get("event_type") in ("fulfill_taker", "fulfill_maker")
    ]
    for t in new_trades:
        t["_alias"] = alias
    return sorted(new_trades, key=lambda x: x.get("created_at", 0))


# ══════════════════════════════════════════════════════════
# 포지션 추적기 — direction별 독립 관리
# ══════════════════════════════════════════════════════════
class TraderPositionTracker:
    """
    트레이더 포지션 상태 추적
    key: (symbol, direction) — 롱/숏 완전 독립
    """

    def __init__(self, alias: str):
        self.alias = alias
        # (sym, direction) → {"size": float, "entry_price": float}
        self.positions: dict[tuple, dict] = {}

    def _wavg(self, old_ep: float, old_sz: float, new_ep: float, new_sz: float) -> float:
        total = old_sz + new_sz
        if total <= 0:
            return new_ep
        return (old_ep * old_sz + new_ep * new_sz) / total

    def process_trade(self, trade: dict) -> Optional[dict]:
        """
        1건 처리 → 이벤트 반환 or None
        이벤트: {"type": "open"|"close", "alias", "symbol", "direction",
                 "size", "price", "entry_price"(close만)}
        """
        sym   = trade.get("symbol", "")
        side  = trade.get("side", "")
        amt   = float(trade.get("amount", 0) or 0)
        price = float(trade.get("entry_price", trade.get("price", 0)) or 0)

        if amt <= 0 or price <= 0 or not sym:
            return None

        if side == "open_long":
            k = (sym, "long")
            if k in self.positions:
                pos = self.positions[k]
                pos["entry_price"] = self._wavg(pos["entry_price"], pos["size"], price, amt)
                pos["size"] += amt
            else:
                self.positions[k] = {"size": amt, "entry_price": price}
            return {"type": "open", "alias": self.alias, "symbol": sym,
                    "direction": "long", "size": amt, "price": price}

        elif side == "open_short":
            k = (sym, "short")
            if k in self.positions:
                pos = self.positions[k]
                pos["entry_price"] = self._wavg(pos["entry_price"], pos["size"], price, amt)
                pos["size"] += amt
            else:
                self.positions[k] = {"size": amt, "entry_price": price}
            return {"type": "open", "alias": self.alias, "symbol": sym,
                    "direction": "short", "size": amt, "price": price}

        elif side == "close_long":
            k = (sym, "long")
            if k in self.positions:
                pos = self.positions[k]
                entry = pos["entry_price"]
                close_sz = min(pos["size"], amt)
                pos["size"] -= close_sz
                if pos["size"] < 1e-8:
                    del self.positions[k]
                return {"type": "close", "alias": self.alias, "symbol": sym,
                        "direction": "long", "size": close_sz,
                        "price": price, "entry_price": entry}

        elif side == "close_short":
            k = (sym, "short")
            if k in self.positions:
                pos = self.positions[k]
                entry = pos["entry_price"]
                close_sz = min(pos["size"], amt)
                pos["size"] -= close_sz
                if pos["size"] < 1e-8:
                    del self.positions[k]
                return {"type": "close", "alias": self.alias, "symbol": sym,
                        "direction": "short", "size": close_sz,
                        "price": price, "entry_price": entry}

        return None


# ══════════════════════════════════════════════════════════
# 전략 엔진
# ══════════════════════════════════════════════════════════
class StrategyEngine:
    def __init__(self, strategy_id: str, db: sqlite3.Connection):
        self.sid    = strategy_id
        self.preset = get_preset(strategy_id)
        self.db     = db
        now_ms      = int(time.time() * 1000)

        # 세션 초기화 또는 복원
        cur = db.cursor()
        cur.execute(
            "SELECT equity, realized_pnl, gross_profit, gross_loss, "
            "total_trades, win_trades, max_drawdown, peak_equity "
            "FROM pt_sessions WHERE strategy=?", (strategy_id,)
        )
        row = cur.fetchone()
        if row:
            (self.equity, self.realized_pnl, self.gross_profit, self.gross_loss,
             self.total, self.wins, self.max_dd, self.peak) = [float(x) for x in row]
            self.total = int(self.total); self.wins = int(self.wins)
        else:
            self.equity = self.peak = INITIAL_CAP
            self.realized_pnl = self.gross_profit = self.gross_loss = self.max_dd = 0.0
            self.total = self.wins = 0
            db.execute(
                "INSERT INTO pt_sessions (strategy, started_at, last_updated, "
                "initial_capital, equity, peak_equity) VALUES (?,?,?,?,?,?)",
                (strategy_id, now_ms, now_ms, INITIAL_CAP, INITIAL_CAP, INITIAL_CAP)
            )

        # 열린 포지션 복원 — key: f"{alias}_{sym}_{direction}"
        self.positions: dict[str, dict] = {}
        cur.execute(
            "SELECT trader_alias, symbol, direction, size, entry_price, "
            "stop_loss_price, take_profit_price, opened_at, id "
            "FROM pt_trades WHERE strategy=? AND closed_at=0",
            (strategy_id,)
        )
        for r in cur.fetchall():
            key = f"{r[0]}_{r[1]}_{r[2]}"
            self.positions[key] = {
                "alias": r[0], "symbol": r[1], "direction": r[2],
                "size": float(r[3]), "entry": float(r[4]),
                "sl": float(r[5]), "tp": float(r[6]),
                "high": float(r[4]),
                "opened_at": int(r[7]), "trade_id": int(r[8]),
            }
        log.info(f"[{self.sid}] 복원: equity=${self.equity:,.2f} "
                 f"pnl=${self.realized_pnl:+.4f} positions={len(self.positions)} trades={self.total}")

    def on_open(self, event: dict):
        alias  = event["alias"]
        sym    = event["symbol"]
        direc  = event["direction"]
        price  = event["price"]
        size   = event["size"]
        key    = f"{alias}_{sym}_{direc}"

        cr      = self.preset.get("copy_ratio", 0.10)
        max_pos = self.preset.get("max_position_usdc", 80.0)
        sl_pct  = self.preset.get("stop_loss_pct", 0.0)
        tp_pct  = self.preset.get("take_profit_pct", 0.0)

        copy_size = size * cr
        notional  = copy_size * price
        if notional > max_pos:
            copy_size = max_pos / price
        if copy_size * price < 5.0:  # 최소 $5
            return

        sl_price = tp_price = 0.0
        if direc == "long":
            if sl_pct > 0: sl_price = price * (1 - sl_pct)
            if tp_pct > 0: tp_price = price * (1 + tp_pct)
        else:
            if sl_pct > 0: sl_price = price * (1 + sl_pct)
            if tp_pct > 0: tp_price = price * (1 - tp_pct)

        now_ms = int(time.time() * 1000)

        if key in self.positions:
            # 추가 진입 — 가중평균
            pos = self.positions[key]
            ns  = pos["size"] + copy_size
            ne  = (pos["entry"] * pos["size"] + price * copy_size) / ns
            self.positions[key]["size"]  = ns
            self.positions[key]["entry"] = ne
            return  # 추가 진입은 별도 trade 레코드 미생성

        cur = self.db.cursor()
        cur.execute(
            "INSERT INTO pt_trades (strategy, trader_alias, symbol, direction, "
            "size, entry_price, stop_loss_price, take_profit_price, opened_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (self.sid, alias, sym, direc, copy_size, price, sl_price, tp_price, now_ms)
        )
        trade_id = cur.lastrowid

        self.positions[key] = {
            "alias": alias, "symbol": sym, "direction": direc,
            "size": copy_size, "entry": price,
            "sl": sl_price, "tp": tp_price,
            "high": price, "opened_at": now_ms, "trade_id": trade_id,
        }
        log.debug(f"[{self.sid}] OPEN {alias} {sym} {direc} "
                  f"${copy_size*price:.2f} @{price:.4f} SL={sl_price:.4f}")

    def on_close(self, event: dict, reason: str = "TRADER_CLOSE"):
        alias = event["alias"]
        sym   = event["symbol"]
        direc = event["direction"]
        price = event["price"]
        key   = f"{alias}_{sym}_{direc}"

        if key not in self.positions:
            return

        pos    = self.positions.pop(key)
        entry  = pos["entry"]
        size   = pos["size"]
        now_ms = int(time.time() * 1000)
        hold   = (now_ms - pos["opened_at"]) / 60000

        if direc == "long":
            gross = (price - entry) * size
        else:
            gross = (entry - price) * size

        fee = (entry * size + price * size) * FEE_RATE / 2
        net = gross - fee
        roi = net / (entry * size) * 100 if entry * size > 0 else 0

        self.realized_pnl += net
        self.equity       += net
        self.total        += 1
        if net > 0:
            self.wins         += 1
            self.gross_profit += gross
        else:
            self.gross_loss   += abs(gross)

        if self.equity > self.peak:
            self.peak = self.equity
        dd = (self.peak - self.equity) / self.peak * 100 if self.peak > 0 else 0
        if dd > self.max_dd:
            self.max_dd = dd

        if pos.get("trade_id"):
            self.db.execute(
                "UPDATE pt_trades SET exit_price=?, gross_pnl=?, net_pnl=?, fee=?, "
                "roi_pct=?, hold_min=?, closed_at=?, close_reason=? WHERE id=?",
                (price, gross, net, fee, roi, hold, now_ms, reason, pos["trade_id"])
            )
        self.db.execute(
            "UPDATE pt_sessions SET equity=?, realized_pnl=?, gross_profit=?, "
            "gross_loss=?, total_trades=?, win_trades=?, max_drawdown=?, "
            "peak_equity=?, last_updated=? WHERE strategy=?",
            (self.equity, self.realized_pnl, self.gross_profit, self.gross_loss,
             self.total, self.wins, self.max_dd, self.peak, now_ms, self.sid)
        )

        emoji = "✅" if net > 0 else "🔴"
        log.info(f"[{self.sid}] {emoji} {alias} {sym} {direc} "
                 f"{entry:.4f}→{price:.4f} ${net:+.4f} ({roi:+.2f}%) [{reason}]")

    def check_stops(self, prices: dict[str, float]):
        sl_pct = self.preset.get("stop_loss_pct", 0.0)
        tp_pct = self.preset.get("take_profit_pct", 0.0)
        tr_pct = self.preset.get("trailing_stop_pct", 0.0)
        if sl_pct == 0 and tp_pct == 0 and tr_pct == 0:
            return

        to_close = []
        for key, pos in list(self.positions.items()):
            sym   = pos["symbol"]
            price = prices.get(sym, 0)
            if price <= 0:
                continue
            entry = pos["entry"]
            direc = pos["direction"]

            if direc == "long":
                roi = (price - entry) / entry
                if price > pos["high"]:
                    self.positions[key]["high"] = price
                trail_dd = (price - pos["high"]) / pos["high"] if pos["high"] > 0 else 0
            else:
                roi = (entry - price) / entry
                if price < pos["high"] or pos["high"] == entry:
                    self.positions[key]["high"] = price
                trail_dd = (pos["high"] - price) / pos["high"] if pos["high"] > 0 else 0

            reason = ""
            if sl_pct > 0 and roi <= -sl_pct:
                reason = f"STOP_LOSS({roi*100:.1f}%)"
            elif tp_pct > 0 and roi >= tp_pct:
                reason = f"TAKE_PROFIT({roi*100:.1f}%)"
            elif tr_pct > 0 and trail_dd <= -tr_pct:
                reason = f"TRAILING({trail_dd*100:.1f}%)"

            if reason:
                to_close.append((key, pos, price, reason))

        for key, pos, price, reason in to_close:
            self.on_close({
                "alias": pos["alias"], "symbol": pos["symbol"],
                "direction": pos["direction"], "price": price,
            }, reason)

    def save_snapshot(self):
        snap_at = (int(time.time()) // SNAP_SEC) * SNAP_SEC * 1000
        wr  = self.wins / self.total * 100 if self.total > 0 else 0
        pf  = self.gross_profit / self.gross_loss if self.gross_loss > 0 else 9.99
        roi = (self.equity - INITIAL_CAP) / INITIAL_CAP * 100
        try:
            self.db.execute(
                "INSERT OR IGNORE INTO pt_snapshots "
                "(strategy, snap_at, equity, realized_pnl, open_positions, "
                "total_trades, win_rate, profit_factor, max_drawdown, roi_pct) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (self.sid, snap_at, self.equity, self.realized_pnl,
                 len(self.positions), self.total, wr, pf, self.max_dd, roi)
            )
        except Exception:
            pass

    def stats(self) -> dict:
        wr  = self.wins / self.total * 100 if self.total > 0 else 0
        pf  = self.gross_profit / self.gross_loss if self.gross_loss > 0 else 0.0
        roi = (self.equity - INITIAL_CAP) / INITIAL_CAP * 100
        return {
            "strategy": self.sid,
            "label":    self.preset.get("label", self.sid),
            "emoji":    self.preset.get("emoji", ""),
            "equity":   round(self.equity, 4),
            "pnl":      round(self.realized_pnl, 4),
            "roi_pct":  round(roi, 4),
            "positions": len(self.positions),
            "trades":   self.total,
            "wins":     self.wins,
            "win_rate": round(wr, 1),
            "pf":       round(pf, 3),
            "max_dd":   round(self.max_dd, 2),
        }


# ══════════════════════════════════════════════════════════
# 대시보드
# ══════════════════════════════════════════════════════════
SEP = "═" * 76
def dashboard(engines: list[StrategyEngine], cycle: int, elapsed_min: float):
    now = datetime.now().strftime("%Y-%m-%d %H:%M KST")
    print(f"\n{SEP}")
    print(f"  🔄 Copy Perp 4전략 페이퍼트레이딩  |  {now}  |  #{cycle}  |  {elapsed_min:.0f}분")
    print(SEP)
    print(f"  {'전략':<14} {'자본':>10} {'실현PnL':>11} {'ROI':>8} {'포지션':>6} "
          f"{'거래':>6} {'WR':>6} {'PF':>6} {'MDD':>6}")
    print(f"  {'─'*74}")

    best_roi = max(e.stats()["roi_pct"] for e in engines)
    for e in engines:
        s = e.stats()
        mark = "★" if s["roi_pct"] == best_roi and best_roi > 0 else " "
        sign = "✅" if s["pnl"] > 0 else ("🔴" if s["pnl"] < 0 else "⏳")
        pf_s = f"{s['pf']:.2f}" if s["pf"] > 0 else "-"
        dd_s = f"{s['max_dd']:.1f}%" if s["max_dd"] > 0 else "-"
        print(f"  {sign}{mark} {s['emoji']} {s['label']:<10} ${s['equity']:>9,.2f} "
              f"${s['pnl']:>+9.4f} {s['roi_pct']:>+7.3f}% {s['positions']:>6} "
              f"{s['trades']:>6} {s['win_rate']:>5.1f}% {pf_s:>6} {dd_s:>6}")

    print(SEP)

    # 비교 인사이트
    with_trades = [e for e in engines if e.total > 0]
    if len(with_trades) >= 2:
        best  = max(with_trades, key=lambda e: e.realized_pnl)
        worst = min(with_trades, key=lambda e: e.realized_pnl)
        if best.realized_pnl != worst.realized_pnl:
            diff = best.realized_pnl - worst.realized_pnl
            print(f"  💡 {best.sid}({best.realized_pnl:+.4f}) vs {worst.sid}({worst.realized_pnl:+.4f}) | 차이 ${diff:.4f}")

    # 오픈 포지션 샘플
    sample = []
    for key in list(engines[0].positions.keys())[:4]:
        pos = engines[0].positions[key]
        sample.append(f"{pos['alias'][:8]}_{pos['symbol']} {pos['direction']}")
    if sample:
        print(f"  📊 포지션(기본형): {', '.join(sample)}")
    print()


# ══════════════════════════════════════════════════════════
# 메인 루프
# ══════════════════════════════════════════════════════════
def run():
    # 페이퍼트레이딩 전용 DB
    db = sqlite3.connect(PT_DB_PATH, timeout=60, check_same_thread=False,
                         isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(SCHEMA)

    # mainnet_trades 읽기 전용 DB
    src_db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)

    # 엔진 초기화
    engines = [StrategyEngine(sid, db) for sid in STRATEGIES]

    # 트레이더별 포지션 추적기
    trackers = {alias: TraderPositionTracker(alias) for alias, _ in TRADERS}

    # last_ts 복원
    cur = db.cursor()
    last_ts_map: dict[str, int] = {}
    for alias, _ in TRADERS:
        cur.execute("SELECT last_ts FROM pt_last_ts WHERE trader_alias=?", (alias,))
        row = cur.fetchone()
        last_ts_map[alias] = int(row[0]) if row else 0

    # mainnet_trades 최신 ts
    src_cur = src_db.cursor()
    src_cur.execute("SELECT trader_alias, MAX(created_at) FROM mainnet_trades GROUP BY trader_alias")
    src_max_ts = {r[0]: int(r[1]) for r in src_cur.fetchall()}

    is_fresh = all(v == 0 for v in last_ts_map.values())
    log.info(f"시작 (fresh={is_fresh}) | 전략 {len(engines)}개 | 트레이더 {len(TRADERS)}명 | 폴링 {POLL_SEC}s")

    if is_fresh:
        # 최초: mainnet_trades로 현재 포지션 상태 파악 → 엔진 동기화
        log.info("기존 mainnet_trades 기반 포지션 초기화...")
        for alias, _ in TRADERS:
            src_cur.execute("""
                SELECT symbol, side, amount, entry_price FROM mainnet_trades
                WHERE trader_alias=? AND event_type='fulfill_taker'
                ORDER BY created_at ASC
            """, (alias,))
            for sym, side, amt, price in src_cur.fetchall():
                trackers[alias].process_trade({
                    "symbol": sym, "side": side,
                    "amount": amt, "entry_price": price,
                    "event_type": "fulfill_taker",
                })

        # 트레이커 현재 포지션 → 엔진에 등록
        for alias, _ in TRADERS:
            for (sym, direc), pos in trackers[alias].positions.items():
                event = {
                    "alias": alias, "symbol": sym, "direction": direc,
                    "size": pos["size"], "price": pos["entry_price"],
                }
                for e in engines:
                    key = f"{alias}_{sym}_{direc}"
                    if key not in e.positions:
                        e.on_open(event)
            time.sleep(0.02)

        # last_ts = mainnet_trades 최대값 (이중처리 방지)
        for alias, _ in TRADERS:
            last_ts_map[alias] = src_max_ts.get(alias, 0)
            db.execute(
                "INSERT OR REPLACE INTO pt_last_ts (trader_alias, last_ts) VALUES (?,?)",
                (alias, last_ts_map[alias])
            )
        log.info(f"초기화 완료 | 전략당 포지션: {len(engines[0].positions)}개")
    else:
        # 재시작: last_ts 이후 신규 거래만 처리
        # 기존 열린 포지션 복원 (DB에서 읽음)
        log.info(f"재시작 복원 | 전략당 포지션: {len(engines[0].positions)}개")

    # 현재가 초기화
    src_cur.execute("""
        SELECT symbol, entry_price FROM mainnet_trades
        WHERE created_at=(
            SELECT MAX(created_at) FROM mainnet_trades m2
            WHERE m2.symbol=mainnet_trades.symbol
        ) AND entry_price > 0
    """)
    prices: dict[str, float] = {r[0]: float(r[1]) for r in src_cur.fetchall()}

    cycle = 0
    start_time = time.time()
    last_snap  = start_time
    running    = True

    def _stop(sig, frame):
        nonlocal running
        log.info("종료 신호 수신")
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT,  _stop)

    while running:
        cycle += 1
        now_ms = int(time.time() * 1000)

        # ── 신규 거래 수집 ───────────────────────────────
        new_events: list[dict] = []
        for alias, addr in TRADERS:
            try:
                trades = fetch_new_trades(alias, addr, last_ts_map[alias])
                if trades:
                    for t in trades:
                        ev = trackers[alias].process_trade(t)
                        if ev:
                            new_events.append(ev)
                    last_ts_map[alias] = max(t.get("created_at", 0) for t in trades)
                    db.execute(
                        "INSERT OR REPLACE INTO pt_last_ts (trader_alias, last_ts) VALUES (?,?)",
                        (alias, last_ts_map[alias])
                    )
                time.sleep(0.3)
            except Exception as ex:
                log.warning(f"수집 오류 {alias}: {ex}")

        # ── 이벤트 → 4전략 동시 반영 ─────────────────────
        for ev in new_events:
            for engine in engines:
                try:
                    if ev["type"] == "open":
                        engine.on_open(ev)
                    else:
                        engine.on_close(ev, "TRADER_CLOSE")
                except Exception as ex:
                    log.error(f"[{engine.sid}] 처리 오류: {ex}")

        # ── 현재가 갱신 (mainnet_trades 최신) ────────────
        try:
            src_cur2 = src_db.cursor()
            src_cur2.execute("""
                SELECT symbol, entry_price FROM mainnet_trades
                WHERE created_at=(
                    SELECT MAX(created_at) FROM mainnet_trades m2
                    WHERE m2.symbol=mainnet_trades.symbol
                ) AND entry_price > 0
            """)
            prices = {r[0]: float(r[1]) for r in src_cur2.fetchall()}
        except Exception:
            pass

        # ── 손절/익절/트레일링 체크 ──────────────────────
        for engine in engines:
            try:
                engine.check_stops(prices)
            except Exception as ex:
                log.error(f"[{engine.sid}] 손절 오류: {ex}")

        # ── 스냅샷 ────────────────────────────────────────
        if time.time() - last_snap >= SNAP_SEC:
            for engine in engines:
                engine.save_snapshot()
            last_snap = time.time()

        # ── 대시보드 ──────────────────────────────────────
        elapsed = (time.time() - start_time) / 60
        dashboard(engines, cycle, elapsed)
        if new_events:
            log.info(f"cycle#{cycle}: {len(new_events)}건 이벤트 처리")

        time.sleep(POLL_SEC)

    db.close()
    log.info("종료 완료")


if __name__ == "__main__":
    run()
