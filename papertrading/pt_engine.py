"""
Copy Perp — 4전략 통합 페이퍼트레이딩 엔진 v2
═══════════════════════════════════════════════════════════════════════
구조:
  ① 30초마다 mainnet /trades/history 신규 거래 수집
  ② 수집 즉시 open/close 이벤트 파싱 → 포지션 delta 계산
  ③ 4전략 엔진에 동시 반영 (copy_ratio / SL / TP / trailing 독립 적용)
  ④ 청산 발생 시 즉시 PnL 기록 → DB 누적
  ⑤ 매 사이클 대시보드 출력 + 5분마다 스냅샷

핵심 원칙:
  - 같은 이벤트를 4전략이 독립 처리 (전략별 copy_ratio/SL 차등)
  - 시간이 쌓일수록 전략 간 PnL 격차 + 손절 효과 데이터 확보
  - 재시작 시 DB에서 세션 완전 복원
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
from collections import defaultdict
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
DB_PATH        = os.path.join(_ROOT, "copy_perp.db")        # mainnet_trades 읽기용
PT_DB_PATH     = os.path.join(_ROOT, "pt_paper.db")         # 페이퍼트레이딩 전용 쓰기 DB
POLL_SEC       = 30     # 거래 수집 주기
SNAP_SEC       = 300    # 스냅샷 주기 (5분)
INITIAL_CAP    = 10_000.0
STRATEGIES     = ["default", "conservative", "balanced", "aggressive"]

# 추적 트레이더 (mainnet CARP 상위)
TRADERS = [
    ("YjCD9Gek", "YjCD9Gek6MVY9t3MLEGYYdZLeaF6MZrpgZraayWsv9E"),
    ("GTU92nBC",  "GTU92nBC8LMyt9W4Qqc319BFR1vpkNNPAbt4QCnX7kZ6"),
    ("3iKDU1jU",  "3iKDU1jUU1KrJXFkYuQBRUALSFKbnWUFjx1o8E7VqxhG"),
    ("5RX2DD42",  "5RX2DD425DHjJHJWYSiJcFh7BsRb6b66UFYSmB2jJBHs"),
    ("4TYEjn9P",  "4TYEjn9PSpxoBNBXWgvUGaqQ8B4sNHRcLUEbA9mHzPfZ"),
    ("HtC4WT6J",  "HtC4WT6JhKz8eojNbkpAv16j5mB6JBj3y8EVbuVzHkCZ"),
]

_log_handler = logging.FileHandler("/tmp/pt_engine.log")
_log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
_stdout_handler = logging.StreamHandler(sys.stdout)
_stdout_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logging.root.setLevel(logging.INFO)
logging.root.handlers = [_log_handler, _stdout_handler]
log = logging.getLogger("pt")

# ── DB 초기화 ─────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS pt_sessions (
    strategy        TEXT PRIMARY KEY,
    started_at      INTEGER NOT NULL,
    last_updated    INTEGER NOT NULL,
    initial_capital REAL    DEFAULT 10000,
    equity          REAL    DEFAULT 10000,
    realized_pnl    REAL    DEFAULT 0,
    unrealized_pnl  REAL    DEFAULT 0,
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
    direction       TEXT    NOT NULL,   -- 'long' / 'short'
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
    unrealized_pnl  REAL    DEFAULT 0,
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

FEE_RATE = 0.0015  # taker 0.05% + builder 0.10% = 0.15% per trade


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
    """last_ts 이후 신규 거래만 반환 (최대 500건)"""
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
# 포지션 추적 (트레이더별)
# ══════════════════════════════════════════════════════════
class TraderPositionTracker:
    """트레이더 포지션 상태 추적 → open/close 이벤트 추출"""

    def __init__(self, alias: str):
        self.alias = alias
        # sym → {direction, size, entry_price}
        self.positions: dict[str, dict] = {}

    def process_trade(self, trade: dict) -> Optional[dict]:
        """
        거래 1건 처리 → 이벤트 반환
        이벤트 형식:
          {"type": "open"|"close", "alias": ..., "symbol": ...,
           "direction": "long"|"short", "size": ..., "price": ...,
           "pnl_per_unit": float (close만)}
        """
        sym   = trade.get("symbol", "")
        side  = trade.get("side", "")
        amt   = float(trade.get("amount", 0) or 0)
        price = float(trade.get("entry_price", trade.get("price", 0)) or 0)

        if amt <= 0 or price <= 0 or not sym:
            return None

        pos = self.positions.get(sym)

        if side == "open_long":
            if pos and pos["direction"] == "long":
                # 추가 진입 — 가중평균
                ns = pos["size"] + amt
                ne = (pos["entry_price"] * pos["size"] + price * amt) / ns
                self.positions[sym] = {"direction": "long", "size": ns, "entry_price": ne}
            else:
                self.positions[sym] = {"direction": "long", "size": amt, "entry_price": price}
            return {"type": "open", "alias": self.alias, "symbol": sym,
                    "direction": "long", "size": amt, "price": price}

        elif side == "open_short":
            if pos and pos["direction"] == "short":
                ns = pos["size"] + amt
                ne = (pos["entry_price"] * pos["size"] + price * amt) / ns
                self.positions[sym] = {"direction": "short", "size": ns, "entry_price": ne}
            else:
                self.positions[sym] = {"direction": "short", "size": amt, "entry_price": price}
            return {"type": "open", "alias": self.alias, "symbol": sym,
                    "direction": "short", "size": amt, "price": price}

        elif side == "close_long":
            if pos and pos["direction"] == "long":
                ppu = price - pos["entry_price"]  # pnl per unit
                close_size = min(pos["size"], amt)
                pos["size"] -= close_size
                if pos["size"] < 1e-8:
                    del self.positions[sym]
                return {"type": "close", "alias": self.alias, "symbol": sym,
                        "direction": "long", "size": close_size, "price": price,
                        "entry_price": pos["entry_price"], "pnl_per_unit": ppu}

        elif side == "close_short":
            if pos and pos["direction"] == "short":
                ppu = pos["entry_price"] - price
                close_size = min(pos["size"], amt)
                pos["size"] -= close_size
                if pos["size"] < 1e-8:
                    del self.positions[sym]
                return {"type": "close", "alias": self.alias, "symbol": sym,
                        "direction": "short", "size": close_size, "price": price,
                        "entry_price": pos["entry_price"], "pnl_per_unit": ppu}

        return None


# ══════════════════════════════════════════════════════════
# 전략 엔진
# ══════════════════════════════════════════════════════════
class StrategyEngine:
    """단일 전략 페이퍼트레이딩 엔진"""

    def __init__(self, strategy_id: str, db: sqlite3.Connection):
        self.sid    = strategy_id
        self.preset = get_preset(strategy_id)
        self.db     = db
        db.execute("PRAGMA busy_timeout=30000")
        now_ms      = int(time.time() * 1000)

        # 세션 초기화 또는 복원
        cur = db.cursor()
        cur.execute("SELECT equity, realized_pnl, gross_profit, gross_loss, "
                    "total_trades, win_trades, max_drawdown, peak_equity "
                    "FROM pt_sessions WHERE strategy=?", (strategy_id,))
        row = cur.fetchone()
        if row:
            (self.equity, self.realized_pnl, self.gross_profit, self.gross_loss,
             self.total, self.wins, self.max_dd, self.peak) = [float(x) for x in row]
            self.total = int(self.total); self.wins = int(self.wins)
        else:
            self.equity = self.peak = INITIAL_CAP
            self.realized_pnl = self.gross_profit = self.gross_loss = self.max_dd = 0.0
            self.total = self.wins = 0
            cur.execute(
                "INSERT INTO pt_sessions (strategy, started_at, last_updated, "
                "initial_capital, equity, peak_equity) VALUES (?,?,?,?,?,?)",
                (strategy_id, now_ms, now_ms, INITIAL_CAP, INITIAL_CAP, INITIAL_CAP)
            )
        
        # 열린 포지션 복원
        self.positions: dict[str, dict] = {}  # key: f"{alias}_{sym}"
        cur.execute(
            "SELECT trader_alias, symbol, direction, size, entry_price, "
            "stop_loss_price, take_profit_price, opened_at, id "
            "FROM pt_trades WHERE strategy=? AND closed_at=0",
            (strategy_id,)
        )
        for r in cur.fetchall():
            key = f"{r[0]}_{r[1]}"
            self.positions[key] = {
                "alias": r[0], "symbol": r[1], "direction": r[2],
                "size": float(r[3]), "entry": float(r[4]),
                "sl": float(r[5]), "tp": float(r[6]),
                "high": float(r[4]),   # trailing 고점 초기화
                "opened_at": int(r[8]), "trade_id": int(r[8]),
            }
        log.info(f"[{self.sid}] 복원: equity=${self.equity:,.2f} pnl=${self.realized_pnl:+.4f} "
                 f"positions={len(self.positions)} trades={self.total}")

    # ── 이벤트 처리 ───────────────────────────────────────
    def on_open(self, event: dict):
        alias  = event["alias"]
        sym    = event["symbol"]
        key    = f"{alias}_{sym}"
        direc  = event["direction"]
        price  = event["price"]
        size   = event["size"]

        cr      = self.preset.get("copy_ratio", 0.10)
        max_pos = self.preset.get("max_position_usdc", 120.0)
        sl_pct  = self.preset.get("stop_loss_pct", 0.0)
        tp_pct  = self.preset.get("take_profit_pct", 0.0)

        copy_size = size * cr
        notional  = copy_size * price
        if notional > max_pos:
            copy_size = max_pos / price

        sl_price = tp_price = 0.0
        if direc == "long":
            if sl_pct > 0: sl_price = price * (1 - sl_pct)
            if tp_pct > 0: tp_price = price * (1 + tp_pct)
        else:
            if sl_pct > 0: sl_price = price * (1 + sl_pct)
            if tp_pct > 0: tp_price = price * (1 - tp_pct)

        now_ms = int(time.time() * 1000)

        if key in self.positions:
            # 추가 진입 — 가중평균 갱신
            pos = self.positions[key]
            ns = pos["size"] + copy_size
            ne = (pos["entry"] * pos["size"] + price * copy_size) / ns
            self.positions[key]["size"] = ns
            self.positions[key]["entry"] = ne
            return  # 별도 trade 레코드 불필요

        self.positions[key] = {
            "alias": alias, "symbol": sym, "direction": direc,
            "size": copy_size, "entry": price,
            "sl": sl_price, "tp": tp_price,
            "high": price, "opened_at": now_ms, "trade_id": None,
        }

        cur = self.db.cursor()
        cur.execute(
            "INSERT INTO pt_trades (strategy, trader_alias, symbol, direction, "
            "size, entry_price, stop_loss_price, take_profit_price, opened_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (self.sid, alias, sym, direc, copy_size, price, sl_price, tp_price, now_ms)
        )
        self.positions[key]["trade_id"] = cur.lastrowid

        log.info(f"[{self.sid}] OPEN {alias} {sym} {direc} "
                 f"size={copy_size:.4f} @{price:.4f} SL={sl_price:.4f} TP={tp_price:.4f}")

    def on_close(self, event: dict, reason: str = "TRADER_CLOSE"):
        alias = event["alias"]
        sym   = event["symbol"]
        key   = f"{alias}_{sym}"
        price = event["price"]

        if key not in self.positions:
            return

        pos    = self.positions.pop(key)
        entry  = pos["entry"]
        size   = pos["size"]
        direc  = pos["direction"]
        now_ms = int(time.time() * 1000)
        hold   = (now_ms - pos["opened_at"]) / 60000

        if direc == "long":
            gross = (price - entry) * size
        else:
            gross = (entry - price) * size

        fee = abs(gross) * FEE_RATE + (entry * size * FEE_RATE)  # 진입+청산 수수료
        net = gross - fee
        roi = net / (entry * size) * 100 if entry * size > 0 else 0

        self.realized_pnl += net
        self.equity       += net
        self.total        += 1
        if net > 0:
            self.wins       += 1
            self.gross_profit += gross
        else:
            self.gross_loss   += abs(gross)

        # MDD 갱신
        if self.equity > self.peak:
            self.peak = self.equity
        dd = (self.peak - self.equity) / self.peak * 100 if self.peak > 0 else 0
        if dd > self.max_dd:
            self.max_dd = dd

        # DB 기록
        cur = self.db.cursor()
        if pos.get("trade_id"):
            cur.execute(
                "UPDATE pt_trades SET exit_price=?, gross_pnl=?, net_pnl=?, fee=?, "
                "roi_pct=?, hold_min=?, closed_at=?, close_reason=? WHERE id=?",
                (price, gross, net, fee, roi, hold, now_ms, reason, pos["trade_id"])
            )
        cur.execute(
            "UPDATE pt_sessions SET equity=?, realized_pnl=?, gross_profit=?, "
            "gross_loss=?, total_trades=?, win_trades=?, max_drawdown=?, "
            "peak_equity=?, last_updated=? WHERE strategy=?",
            (self.equity, self.realized_pnl, self.gross_profit, self.gross_loss,
             self.total, self.wins, self.max_dd, self.peak, now_ms, self.sid)
        )

        emoji = "✅" if net > 0 else "🔴"
        log.info(f"[{self.sid}] {emoji} CLOSE {alias} {sym} {direc} "
                 f"@{price:.4f} pnl=${net:+.4f} ({roi:+.2f}%) hold={hold:.1f}min [{reason}]")

    # ── 손절/익절/트레일링 체크 ──────────────────────────
    def check_stops(self, cur_prices: dict[str, float]):
        sl_pct = self.preset.get("stop_loss_pct", 0.0)
        tp_pct = self.preset.get("take_profit_pct", 0.0)
        tr_pct = self.preset.get("trailing_stop_pct", 0.0)

        to_close = []
        for key, pos in self.positions.items():
            sym   = pos["symbol"]
            price = cur_prices.get(sym, 0)
            if price <= 0:
                continue

            entry = pos["entry"]
            direc = pos["direction"]

            if direc == "long":
                roi = (price - entry) / entry
                # 트레일링 고점 갱신
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
            event = {
                "alias": pos["alias"], "symbol": pos["symbol"],
                "direction": pos["direction"], "price": price,
                "entry_price": pos["entry"],
            }
            self.on_close(event, reason)

    def save_snapshot(self):
        snap_at = (int(time.time()) // SNAP_SEC) * SNAP_SEC * 1000
        wr = self.wins / self.total * 100 if self.total > 0 else 0
        pf = self.gross_profit / self.gross_loss if self.gross_loss > 0 else 9.99
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
            "strategy":   self.sid,
            "label":      self.preset.get("label", self.sid),
            "emoji":      self.preset.get("emoji", ""),
            "equity":     round(self.equity, 4),
            "pnl":        round(self.realized_pnl, 4),
            "roi_pct":    round(roi, 4),
            "positions":  len(self.positions),
            "trades":     self.total,
            "wins":       self.wins,
            "win_rate":   round(wr, 1),
            "pf":         round(pf, 3),
            "max_dd":     round(self.max_dd, 2),
        }


# ══════════════════════════════════════════════════════════
# 대시보드 출력
# ══════════════════════════════════════════════════════════
SEP = "═" * 74
def dashboard(engines: list[StrategyEngine], cycle: int, elapsed_min: float):
    now = datetime.now().strftime("%Y-%m-%d %H:%M KST")
    print(f"\n{SEP}")
    print(f"  🔄 Copy Perp 4전략 페이퍼트레이딩  |  {now}  |  cycle#{cycle}  |  {elapsed_min:.0f}분 경과")
    print(SEP)
    print(f"  {'전략':<14} {'자본':>10} {'실현PnL':>12} {'ROI':>8} {'포지션':>6} {'거래':>5} "
          f"{'WR':>6} {'PF':>6} {'MDD':>6}")
    print(f"  {'─'*72}")

    best_roi = max(e.stats()["roi_pct"] for e in engines)
    for e in engines:
        s = e.stats()
        mark = "★" if s["roi_pct"] == best_roi and best_roi > 0 else " "
        sign = "✅" if s["pnl"] > 0 else ("🔴" if s["pnl"] < 0 else "⏳")
        pf_s = f"{s['pf']:.2f}" if s['pf'] > 0 else "-"
        dd_s = f"{s['max_dd']:.1f}%" if s['max_dd'] > 0 else "-"
        print(f"  {sign}{mark} {s['emoji']} {s['label']:<10} ${s['equity']:>9,.2f} "
              f"${s['pnl']:>+10.4f} {s['roi_pct']:>+7.3f}% {s['positions']:>6} "
              f"{s['trades']:>5} {s['win_rate']:>5.1f}% {pf_s:>6} {dd_s:>6}")

    print(SEP)

    # 전략 비교 인사이트
    all_closed = [e for e in engines if e.total > 0]
    if all_closed:
        best = max(all_closed, key=lambda e: e.realized_pnl)
        worst = min(all_closed, key=lambda e: e.realized_pnl)
        if best.realized_pnl != worst.realized_pnl:
            diff = best.realized_pnl - worst.realized_pnl
            print(f"  💡 최고: {best.sid}({best.realized_pnl:+.4f}) vs 최저: {worst.sid}({worst.realized_pnl:+.4f}) | 차이: ${diff:.4f}")

    # 오픈 포지션 요약 (상위 5개)
    all_pos = []
    for e in engines[:1]:  # default만 샘플
        for key, pos in list(e.positions.items())[:5]:
            all_pos.append(f"{pos['alias']}_{pos['symbol']} {pos['direction']}")
    if all_pos:
        print(f"  📊 오픈포지션 샘플: {', '.join(all_pos)}")
    print()


# ══════════════════════════════════════════════════════════
# 메인 루프
# ══════════════════════════════════════════════════════════
def run():
    # 페이퍼트레이딩 전용 DB (copy_perp.db와 분리 → lock 없음)
    db = sqlite3.connect(PT_DB_PATH, timeout=60, check_same_thread=False,
                         isolation_level=None)  # autocommit — 개별 쿼리마다 즉시 commit
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(SCHEMA)

    # mainnet_trades 읽기용 DB (READ ONLY)
    src_db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)

    # 엔진 초기화
    engines = [StrategyEngine(sid, db) for sid in STRATEGIES]

    # 트레이더 포지션 추적기 + 마지막 수집 ts
    trackers = {alias: TraderPositionTracker(alias) for alias, _ in TRADERS}

    # DB에서 last_ts 복원 (pt_paper.db에서)
    cur = db.cursor()
    last_ts_map: dict[str, int] = {}
    for alias, _ in TRADERS:
        cur.execute("SELECT last_ts FROM pt_last_ts WHERE trader_alias=?", (alias,))
        row = cur.fetchone()
        last_ts_map[alias] = int(row[0]) if row else 0

    # mainnet_trades 최신 ts 조회 (이중처리 방지용)
    src_cur = src_db.cursor()
    src_cur.execute("SELECT trader_alias, MAX(created_at) FROM mainnet_trades GROUP BY trader_alias")
    src_max_ts = {r[0]: int(r[1]) for r in src_cur.fetchall()}

    # 기존 mainnet_trades에서 포지션 초기 복원 (last_ts=0인 경우만 — 최초 실행)
    is_fresh_start = all(v == 0 for v in last_ts_map.values())
    log.info(f"기존 mainnet_trades 기반 포지션 초기화 (fresh={is_fresh_start})...")
    if is_fresh_start:
        for alias, addr in TRADERS:
            src_cur.execute("""
                SELECT symbol, side, amount, entry_price FROM mainnet_trades
                WHERE trader_alias=? AND event_type='fulfill_taker'
                ORDER BY created_at ASC
            """, (alias,))
            for sym, side, amt, price in src_cur.fetchall():
                fake = {"symbol": sym, "side": side,
                        "amount": amt, "entry_price": price,
                        "event_type": "fulfill_taker"}
                trackers[alias].process_trade(fake)
        # 최초 실행 시 last_ts를 현재 mainnet_trades 최대값으로 세팅
        for alias, _ in TRADERS:
            last_ts_map[alias] = src_max_ts.get(alias, 0)
            db.execute(
                "INSERT OR REPLACE INTO pt_last_ts (trader_alias, last_ts) VALUES (?,?)",
                (alias, last_ts_map[alias])
            )
        log.info("last_ts 초기화 완료 — 이후 신규 거래만 수집")
    else:
        log.info("재시작 감지 — 기존 last_ts 유지, 신규 거래만 수집")

    # 현재가 (mainnet_trades 최신 가격)
    src_cur.execute("""
        SELECT symbol, entry_price FROM mainnet_trades
        WHERE created_at=(
            SELECT MAX(created_at) FROM mainnet_trades m2
            WHERE m2.symbol=mainnet_trades.symbol
        ) AND entry_price > 0
    """)
    prices: dict[str, float] = {r[0]: float(r[1]) for r in src_cur.fetchall()}

    # 시작 시 기존 열린 포지션 동기화 (배치 처리 — lock 방지)
    now_ms_init = int(time.time() * 1000)
    for alias, addr in TRADERS:
        existing = trackers[alias].positions
        for sym, tpos in existing.items():
            event = {
                "alias": alias, "symbol": sym,
                "direction": tpos["direction"],
                "size": tpos["size"], "price": tpos["entry_price"],
            }
            for e in engines:
                # 이미 DB에 있는 포지션은 스킵
                key = f"{alias}_{sym}"
                if key not in e.positions:
                    e.on_open(event)
        time.sleep(0.05)  # 배치 간 짧은 대기

    log.info(f"시작: {len(engines)}개 전략 | {len(TRADERS)}개 트레이더 추적 | {POLL_SEC}초 폴링")

    cycle = 0
    start_time = time.time()
    last_snap = start_time
    running = True

    def _stop(sig, frame):
        nonlocal running
        log.info("종료 신호")
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT,  _stop)

    while running:
        cycle += 1
        now_ms = int(time.time() * 1000)

        # ── 신규 거래 수집 + 이벤트 처리 ─────────────────
        new_events: list[dict] = []
        for alias, addr in TRADERS:
            try:
                new_trades = fetch_new_trades(alias, addr, last_ts_map[alias])
                if new_trades:
                    log.debug(f"  {alias}: {len(new_trades)}건 신규")
                    for t in new_trades:
                        event = trackers[alias].process_trade(t)
                        if event:
                            new_events.append(event)
                    # last_ts 갱신
                    last_ts_map[alias] = max(t.get("created_at", 0) for t in new_trades)
                    db.execute(
                        "INSERT OR REPLACE INTO pt_last_ts (trader_alias, last_ts) VALUES (?,?)",
                        (alias, last_ts_map[alias])
                    )
                time.sleep(0.3)
            except Exception as ex:
                log.warning(f"수집 오류 {alias}: {ex}")

        # ── 이벤트 → 4전략 동시 반영 ─────────────────────
        for event in new_events:
            for engine in engines:
                try:
                    if event["type"] == "open":
                        engine.on_open(event)
                    elif event["type"] == "close":
                        engine.on_close(event, "TRADER_CLOSE")
                except Exception as ex:
                    log.error(f"[{engine.sid}] 이벤트 처리 오류: {ex}")

        # ── 현재가 갱신 + 손절 체크 ──────────────────────
        # mainnet_trades 최신 가격 갱신 (읽기 전용 DB)
        src_cur2 = src_db.cursor()
        src_cur2.execute("""
            SELECT symbol, entry_price FROM mainnet_trades
            WHERE created_at=(
                SELECT MAX(created_at) FROM mainnet_trades m2
                WHERE m2.symbol=mainnet_trades.symbol
            ) AND entry_price > 0
        """)
        prices = {r[0]: float(r[1]) for r in src_cur2.fetchall()}

        for engine in engines:
            try:
                engine.check_stops(prices)
            except Exception as ex:
                log.error(f"[{engine.sid}] 손절 체크 오류: {ex}")

        # ── 스냅샷 ────────────────────────────────────────
        if time.time() - last_snap >= SNAP_SEC:
            for engine in engines:
                engine.save_snapshot()
            last_snap = time.time()

        # ── 대시보드 ──────────────────────────────────────
        elapsed = (time.time() - start_time) / 60
        dashboard(engines, cycle, elapsed)

        if new_events:
            log.info(f"cycle#{cycle}: {len(new_events)}개 이벤트 처리")
        else:
            log.debug(f"cycle#{cycle}: 신규 이벤트 없음")

        # 다음 사이클
        time.sleep(POLL_SEC)

    db.close()
    log.info("종료")


if __name__ == "__main__":
    run()
