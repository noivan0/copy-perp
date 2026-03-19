"""
Copy Perp — 4전략 통합 페이퍼트레이딩 엔진 v4
═══════════════════════════════════════════════════════════════════════
설계 원칙 (대장 지시 반영):
  - trades history replay 폐기 → /positions API 폴링으로 교체
  - 동일 심볼 롱+숏 동시 보유 = 정상 전략 (마켓메이킹 + 스노우볼)
  - 포지션 변화 감지:
      * 새 포지션 → OPEN 이벤트
      * 포지션 소멸 → CLOSE 이벤트 (entry_price 기반 PnL 계산)
      * amount 증가 → SCALE_IN (추가 진입)
      * amount 감소 → PARTIAL_CLOSE (부분 청산)
  - 4전략 독립 적용 (copy_ratio / SL / TP / trailing)
  - 30초 폴링 → 실시간에 가까운 포지션 추적

포지션 키: (alias, symbol, side)  — bid/ask 독립
PnL 계산: close price (현재가) vs entry_price
"""

from __future__ import annotations

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
DB_PATH        = os.path.join(_ROOT, "copy_perp.db")        # mainnet_trades 읽기용 (현재가)
PT_DB_PATH     = os.path.join(_ROOT, "pt_paper.db")         # 페이퍼트레이딩 전용

POLL_SEC       = 30       # 포지션 폴링 주기
SNAP_SEC       = 300      # 스냅샷 주기 (5분)
INITIAL_CAP    = 10_000.0
STRATEGIES     = ["default", "conservative", "balanced", "aggressive"]
FEE_RATE       = 0.0015   # 진입+청산 총 수수료 (0.15%)

TRADERS = [
    ("YjCD9Gek", "YjCD9Gek6MVY9t3MLEGYYdZLeaF6MZrpgZraayWsv9E"),
    ("GTU92nBC",  "GTU92nBC8LMyt9W4Qqc319BFR1vpkNNPAbt4QCnX7kZ6"),
    ("3iKDU1jU",  "3iKDU1jUU1KrJXFkYuQBRUALSFKbnWUFjx1o8E7VqxhG"),
    ("5RX2DD42",  "5RX2DD425DHjJHJWYSiJcFh7BsRb6b66UFYSmB2jJBHs"),
    ("4TYEjn9P",  "4TYEjn9PSpxoBNBXWgvUGaqQ8B4sNHRcLUEbA9mHzPfZ"),
    ("HtC4WT6J",  "HtC4WT6JhKz8eojNbkpAv16j5mB6JBj3y8EVbuVzHkCZ"),
]

# 로그
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
_fh  = logging.FileHandler("/tmp/pt_engine.log")
_fh.setFormatter(_fmt)
_sh  = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
logging.root.setLevel(logging.INFO)
logging.root.handlers = [_fh, _sh]
# 루트 핸들러만 사용 (propagation 차단)
for name in list(logging.Logger.manager.loggerDict):
    logging.getLogger(name).handlers = []
    logging.getLogger(name).propagate = True
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
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy          TEXT    NOT NULL,
    trader_alias      TEXT    NOT NULL,
    symbol            TEXT    NOT NULL,
    direction         TEXT    NOT NULL,   -- 'long' / 'short'
    size              REAL    NOT NULL,
    entry_price       REAL    NOT NULL,
    exit_price        REAL    DEFAULT 0,
    gross_pnl         REAL    DEFAULT 0,
    net_pnl           REAL    DEFAULT 0,
    fee               REAL    DEFAULT 0,
    roi_pct           REAL    DEFAULT 0,
    hold_min          REAL    DEFAULT 0,
    opened_at         INTEGER NOT NULL,
    closed_at         INTEGER DEFAULT 0,
    close_reason      TEXT    DEFAULT '',
    stop_loss_price   REAL    DEFAULT 0,
    take_profit_price REAL    DEFAULT 0
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
CREATE INDEX IF NOT EXISTS idx_pt_trades_strategy ON pt_trades(strategy, closed_at DESC);
"""


# ══════════════════════════════════════════════════════════
# API
# ══════════════════════════════════════════════════════════
def pm_get(path: str, params: dict = None) -> Optional[dict | list]:
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        r = requests.get(PROXY + urllib.parse.quote(url), timeout=20)
        return r.json() if r.ok else None
    except Exception as e:
        log.debug(f"API 오류 {path}: {e}")
        return None


def fetch_positions(address: str) -> list[dict]:
    """
    /positions?account=<addr> → 현재 열린 포지션 리스트
    응답: [{symbol, side(bid/ask), amount, entry_price, updated_at, ...}]
    """
    r = pm_get("/positions", {"account": address})
    if not r:
        return []
    data = (r.get("data") or []) if isinstance(r, dict) else (r or [])
    result = []
    for pos in data:
        amt = float(pos.get("amount", 0) or 0)
        ep  = float(pos.get("entry_price", 0) or 0)
        if amt > 0 and ep > 0:
            result.append({
                "symbol":      pos.get("symbol", ""),
                "side":        pos.get("side", ""),     # "bid"=long, "ask"=short
                "amount":      amt,
                "entry_price": ep,
                "updated_at":  pos.get("updated_at", 0),
            })
    return result


def fetch_mark_prices() -> dict[str, float]:
    """mainnet_trades DB에서 최신 현재가 조회 (빠름)"""
    try:
        src = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
        cur = src.cursor()
        cur.execute("""
            SELECT symbol, entry_price FROM mainnet_trades
            WHERE created_at=(
                SELECT MAX(created_at) FROM mainnet_trades m2
                WHERE m2.symbol=mainnet_trades.symbol
            ) AND entry_price > 0
        """)
        prices = {r[0]: float(r[1]) for r in cur.fetchall()}
        src.close()
        return prices
    except Exception:
        return {}


# ══════════════════════════════════════════════════════════
# 포지션 변화 감지기
# ══════════════════════════════════════════════════════════
class PositionDiffTracker:
    """
    이전 폴링 결과 vs 현재 폴링 결과를 비교해 이벤트 생성
    포지션 키: (symbol, side)  — bid/ask 독립
    """

    def __init__(self, alias: str):
        self.alias = alias
        # (sym, side) → {"amount": float, "entry_price": float}
        self.prev: dict[tuple, dict] = {}

    def diff(self, current_positions: list[dict]) -> list[dict]:
        """
        현재 포지션과 이전 포지션을 비교 → 이벤트 리스트 반환
        이벤트 타입:
          - OPEN: 새 포지션 등장
          - CLOSE: 포지션 소멸
          - SCALE_IN: amount 증가 (추가 진입)
          - PARTIAL_CLOSE: amount 감소 (부분 청산)
        """
        curr: dict[tuple, dict] = {}
        for pos in current_positions:
            k = (pos["symbol"], pos["side"])
            curr[k] = pos

        events = []
        # 새 포지션 or 변화
        for k, pos in curr.items():
            sym, side = k
            direction = "long" if side == "bid" else "short"
            if k not in self.prev:
                events.append({
                    "type":        "OPEN",
                    "alias":       self.alias,
                    "symbol":      sym,
                    "direction":   direction,
                    "side":        side,
                    "amount":      pos["amount"],
                    "entry_price": pos["entry_price"],
                })
            else:
                prev_pos = self.prev[k]
                delta = pos["amount"] - prev_pos["amount"]
                if delta > prev_pos["amount"] * 0.01:   # 1% 이상 증가
                    events.append({
                        "type":        "SCALE_IN",
                        "alias":       self.alias,
                        "symbol":      sym,
                        "direction":   direction,
                        "side":        side,
                        "amount":      pos["amount"],
                        "delta":       delta,
                        "entry_price": pos["entry_price"],
                    })
                elif delta < -prev_pos["amount"] * 0.01:  # 1% 이상 감소
                    events.append({
                        "type":        "PARTIAL_CLOSE",
                        "alias":       self.alias,
                        "symbol":      sym,
                        "direction":   direction,
                        "side":        side,
                        "amount":      pos["amount"],
                        "delta":       abs(delta),
                        "entry_price": prev_pos["entry_price"],
                    })

        # 소멸한 포지션
        for k, prev_pos in self.prev.items():
            if k not in curr:
                sym, side = k
                direction = "long" if side == "bid" else "short"
                events.append({
                    "type":        "CLOSE",
                    "alias":       self.alias,
                    "symbol":      sym,
                    "direction":   direction,
                    "side":        side,
                    "amount":      prev_pos["amount"],
                    "entry_price": prev_pos["entry_price"],
                })

        self.prev = curr
        return events


# ══════════════════════════════════════════════════════════
# 전략 엔진
# ══════════════════════════════════════════════════════════
class StrategyEngine:
    def __init__(self, strategy_id: str, db: sqlite3.Connection):
        self.sid    = strategy_id
        self.preset = get_preset(strategy_id)
        self.db     = db
        now_ms      = int(time.time() * 1000)

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
            self.total = int(self.total)
            self.wins  = int(self.wins)
        else:
            self.equity = self.peak = INITIAL_CAP
            self.realized_pnl = self.gross_profit = self.gross_loss = self.max_dd = 0.0
            self.total = self.wins = 0
            db.execute(
                "INSERT INTO pt_sessions (strategy, started_at, last_updated, "
                "initial_capital, equity, peak_equity) VALUES (?,?,?,?,?,?)",
                (strategy_id, now_ms, now_ms, INITIAL_CAP, INITIAL_CAP, INITIAL_CAP)
            )

        # 열린 포지션 복원: key = f"{alias}_{sym}_{direction}"
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
                "alias":     r[0], "symbol": r[1], "direction": r[2],
                "size":      float(r[3]), "entry": float(r[4]),
                "sl":        float(r[5]), "tp": float(r[6]),
                "high":      float(r[4]),
                "opened_at": int(r[7]),  "trade_id": int(r[8]),
            }
        log.info(f"[{self.sid}] 복원: equity=${self.equity:,.2f} "
                 f"pnl=${self.realized_pnl:+.4f} pos={len(self.positions)} trades={self.total}")

    # ── 이벤트 처리 ──────────────────────────────────────
    def on_open(self, event: dict):
        alias  = event["alias"]
        sym    = event["symbol"]
        direc  = event["direction"]
        price  = event["entry_price"]
        size   = event["amount"]
        key    = f"{alias}_{sym}_{direc}"

        cr      = self.preset.get("copy_ratio", 0.10)
        max_pos = self.preset.get("max_position_usdc", 80.0)
        sl_pct  = self.preset.get("stop_loss_pct", 0.0)
        tp_pct  = self.preset.get("take_profit_pct", 0.0)

        copy_size = size * cr
        if copy_size * price > max_pos:
            copy_size = max_pos / price
        if copy_size * price < 5.0:
            return  # 최소 $5

        sl_price = tp_price = 0.0
        if direc == "long":
            if sl_pct > 0: sl_price = price * (1 - sl_pct)
            if tp_pct > 0: tp_price = price * (1 + tp_pct)
        else:
            if sl_pct > 0: sl_price = price * (1 + sl_pct)
            if tp_pct > 0: tp_price = price * (1 - tp_pct)

        now_ms = int(time.time() * 1000)

        if key in self.positions:
            return  # 이미 추적 중 (SCALE_IN은 별도 처리)

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
        notional = copy_size * price
        log.info(f"[{self.sid}] OPEN {alias} {sym} {direc} "
                 f"${notional:.1f} @{price:.4f}"
                 + (f" SL={sl_price:.4f}" if sl_price else ""))

    def on_scale_in(self, event: dict):
        """추가 진입 — 가중평균 진입가 갱신"""
        alias = event["alias"]
        sym   = event["symbol"]
        direc = event["direction"]
        key   = f"{alias}_{sym}_{direc}"
        if key not in self.positions:
            self.on_open(event)  # 포지션 없으면 신규 오픈
            return
        pos   = self.positions[key]
        cr    = self.preset.get("copy_ratio", 0.10)
        delta = event.get("delta", 0) * cr
        price = event["entry_price"]
        ns    = pos["size"] + delta
        ne    = (pos["entry"] * pos["size"] + price * delta) / ns
        self.positions[key]["size"]  = ns
        self.positions[key]["entry"] = ne
        log.debug(f"[{self.sid}] SCALE_IN {alias} {sym} {direc} +{delta:.4f} avgEP={ne:.4f}")

    def on_close(self, event: dict, close_price: float, reason: str = "TRADER_CLOSE"):
        alias = event["alias"]
        sym   = event["symbol"]
        direc = event["direction"]
        key   = f"{alias}_{sym}_{direc}"

        if key not in self.positions:
            return

        pos    = self.positions.pop(key)
        entry  = pos["entry"]
        size   = pos["size"]
        now_ms = int(time.time() * 1000)
        hold   = (now_ms - pos["opened_at"]) / 60000

        if direc == "long":
            gross = (close_price - entry) * size
        else:
            gross = (entry - close_price) * size

        fee = (entry * size + close_price * size) * FEE_RATE / 2
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
                (close_price, gross, net, fee, roi, hold, now_ms, reason, pos["trade_id"])
            )
        self.db.execute(
            "UPDATE pt_sessions SET equity=?, realized_pnl=?, gross_profit=?, "
            "gross_loss=?, total_trades=?, win_trades=?, max_drawdown=?, "
            "peak_equity=?, last_updated=? WHERE strategy=?",
            (self.equity, self.realized_pnl, self.gross_profit, self.gross_loss,
             self.total, self.wins, self.max_dd, self.peak, now_ms, self.sid)
        )
        emoji = "✅" if net > 0 else "🔴"
        log.info(f"[{self.sid}] {emoji} CLOSE {alias} {sym} {direc} "
                 f"{entry:.4f}→{close_price:.4f} ${net:+.4f} ({roi:+.2f}%) "
                 f"hold={hold:.1f}m [{reason}]")

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
                if price > pos["high"]: self.positions[key]["high"] = price
                trail_dd = (price - pos["high"]) / pos["high"] if pos["high"] > 0 else 0
            else:
                roi = (entry - price) / entry
                if price < pos["high"] or pos["high"] == entry:
                    self.positions[key]["high"] = price
                trail_dd = (pos["high"] - price) / pos["high"] if pos["high"] > 0 else 0

            reason = ""
            if sl_pct > 0 and roi <= -sl_pct:
                reason = f"SL({roi*100:.1f}%)"
            elif tp_pct > 0 and roi >= tp_pct:
                reason = f"TP({roi*100:.1f}%)"
            elif tr_pct > 0 and trail_dd <= -tr_pct:
                reason = f"TRAIL({trail_dd*100:.1f}%)"

            if reason:
                to_close.append((pos, price, reason))

        for pos, price, reason in to_close:
            self.on_close({
                "alias": pos["alias"], "symbol": pos["symbol"],
                "direction": pos["direction"],
            }, price, reason)

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
SEP = "═" * 78
def dashboard(engines: list[StrategyEngine], cycle: int, elapsed_min: float):
    now = datetime.now().strftime("%Y-%m-%d %H:%M KST")
    print(f"\n{SEP}")
    print(f"  🔄 Copy Perp 4전략 페이퍼트레이딩 v4  |  {now}  |  #{cycle}  |  {elapsed_min:.0f}분")
    print(SEP)
    print(f"  {'전략':<14} {'자본':>10} {'실현PnL':>11} {'ROI':>8} {'포지션':>6} "
          f"{'거래':>5} {'WR':>6} {'PF':>6} {'MDD':>6}")
    print(f"  {'─'*76}")
    for e in engines:
        s    = e.stats()
        best = max(engines, key=lambda x: x.realized_pnl)
        mark = "★" if e is best and s["pnl"] > 0 else " "
        sign = "✅" if s["pnl"] > 0 else ("🔴" if s["pnl"] < 0 else "⏳")
        pf_s = f"{s['pf']:.2f}" if s["pf"] > 0 else " -  "
        dd_s = f"{s['max_dd']:.1f}%" if s["max_dd"] > 0 else "  -  "
        print(f"  {sign}{mark} {s['emoji']} {s['label']:<10} ${s['equity']:>9,.2f} "
              f"${s['pnl']:>+9.4f} {s['roi_pct']:>+7.3f}% {s['positions']:>6} "
              f"{s['trades']:>5} {s['win_rate']:>5.1f}% {pf_s:>6} {dd_s:>6}")
    print(SEP)

    with_trades = [e for e in engines if e.total > 0]
    if len(with_trades) >= 2:
        best_e  = max(with_trades, key=lambda e: e.realized_pnl)
        worst_e = min(with_trades, key=lambda e: e.realized_pnl)
        if best_e is not worst_e:
            diff = best_e.realized_pnl - worst_e.realized_pnl
            print(f"  💡 {best_e.sid}({best_e.realized_pnl:+.4f}) vs "
                  f"{worst_e.sid}({worst_e.realized_pnl:+.4f}) | 차이 ${diff:.4f}")

    # 기본형 포지션 샘플
    sample = []
    for key, pos in list(engines[0].positions.items())[:5]:
        sample.append(f"{pos['alias'][:8]}·{pos['symbol']}·{pos['direction'][:1].upper()}")
    if sample:
        print(f"  📊 기본형 포지션: {', '.join(sample)}")
    print()


# ══════════════════════════════════════════════════════════
# 메인 루프
# ══════════════════════════════════════════════════════════
def run():
    # 페이퍼트레이딩 전용 DB (autocommit)
    db = sqlite3.connect(PT_DB_PATH, timeout=60, check_same_thread=False,
                         isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(SCHEMA)

    # 전략 엔진 초기화
    engines = [StrategyEngine(sid, db) for sid in STRATEGIES]

    # 트레이더별 포지션 추적기
    trackers = {alias: PositionDiffTracker(alias) for alias, _ in TRADERS}

    # 초기 포지션 로딩 (이전 상태와의 diff 없이 OPEN 이벤트 발생)
    log.info(f"초기 포지션 로딩 | 트레이더 {len(TRADERS)}명...")
    for alias, addr in TRADERS:
        try:
            positions = fetch_positions(addr)
            # 첫 폴링 결과를 prev에 저장 (다음 폴링에서 diff 계산)
            for pos in positions:
                k = (pos["symbol"], pos["side"])
                trackers[alias].prev[k] = pos
            log.info(f"  {alias}: {len(positions)}개 포지션")
            time.sleep(0.5)
        except Exception as ex:
            log.warning(f"  {alias} 초기 로딩 오류: {ex}")

    # 현재 오픈 포지션을 엔진에 등록 (기존 열린 것)
    for alias, addr in TRADERS:
        for (sym, side), pos in trackers[alias].prev.items():
            direc = "long" if side == "bid" else "short"
            event = {
                "alias": alias, "symbol": sym, "direction": direc,
                "amount": pos["amount"], "entry_price": pos["entry_price"],
            }
            for e in engines:
                key = f"{alias}_{sym}_{direc}"
                if key not in e.positions:
                    e.on_open(event)

    log.info(f"시작 완료 | 전략 {len(engines)}개 | 폴링 {POLL_SEC}s | "
             f"기본형 초기 포지션: {len(engines[0].positions)}개")

    cycle = 0
    start_time = time.time()
    last_snap  = start_time
    running    = True

    def _stop(sig, frame):
        nonlocal running
        log.info("종료 신호")
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT,  _stop)

    while running:
        cycle += 1

        # ── 포지션 폴링 + 이벤트 감지 ────────────────────
        all_events: list[dict] = []
        for alias, addr in TRADERS:
            try:
                positions = fetch_positions(addr)
                events    = trackers[alias].diff(positions)
                if events:
                    log.debug(f"  {alias}: {len(events)}개 이벤트")
                all_events.extend(events)
                time.sleep(0.4)
            except Exception as ex:
                log.warning(f"폴링 오류 {alias}: {ex}")

        # ── 현재가 조회 ────────────────────────────────────
        prices = fetch_mark_prices()

        # ── 이벤트 → 4전략 동시 반영 ─────────────────────
        for ev in all_events:
            ev_type = ev["type"]
            for engine in engines:
                try:
                    if ev_type == "OPEN":
                        engine.on_open(ev)
                    elif ev_type == "SCALE_IN":
                        engine.on_scale_in(ev)
                    elif ev_type in ("CLOSE", "PARTIAL_CLOSE"):
                        # 청산 가격 = 현재 마크 가격 (API에서 직접 제공하지 않음)
                        close_px = prices.get(ev["symbol"], ev.get("entry_price", 0))
                        engine.on_close(ev, close_px, ev_type)
                except Exception as ex:
                    log.error(f"[{engine.sid}] 이벤트 처리 오류: {ex}")

        # ── 손절/익절 체크 ────────────────────────────────
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
        if all_events:
            log.info(f"cycle#{cycle}: {len(all_events)}개 이벤트 "
                     f"({sum(1 for e in all_events if e['type']=='OPEN')} OPEN "
                     f"/ {sum(1 for e in all_events if 'CLOSE' in e['type'])} CLOSE)")

        time.sleep(POLL_SEC)

    db.close()
    log.info("종료 완료")


if __name__ == "__main__":
    run()
