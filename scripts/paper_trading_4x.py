#!/usr/bin/env python3
"""
Copy Perp — 4가지 전략 병렬 페이퍼트레이딩 엔진
=================================================
- 4개 전략(default/conservative/balanced/aggressive)을 동시 실행
- codetabs 프록시로 Mainnet 실시간 포지션 감지
- paper_sessions / paper_trades / paper_snapshots DB 영속화
- GET /followers/paper-trading API로 실시간 비교 제공

실행: python3 scripts/paper_trading_4x.py [--interval 초] [--capital 금액]
기본: 60초 폴링, 자본 $10,000
"""

import os, sys, json, time, ssl, asyncio, argparse, logging, sqlite3, urllib.request, urllib.parse
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone

# 프로젝트 루트 추가
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("4x-paper")

# ── 수수료 ──────────────────────────────────────────
TAKER_FEE    = 0.0006   # 0.06%
BUILDER_FEE  = 0.0001   # 0.01%
SLIPPAGE_BPS = 5        # 5 bps (0.05%)

# ── 4가지 전략 정의 (메인넷 2026-03-19 확정) ─────────
# ── 스노우볼 설계 ─────────────────────────────────────────────────────────
# 트레이더의 모든 open/close 이벤트를 100% 추적
# 투자금 = 현재 자산 × copy_ratio (복리: 자산 불면 다음 투자금도 증가)
# max_pos_usdc = 단일 포지션 상한 (자산 보호용)
# 동일 심볼 재진입 허용 = 트레이더가 같은 방향으로 다시 들어오면 재복사
STRATEGIES = {
    "default": {
        "label":        "📋 기본형",
        "copy_ratio":    0.10,        # 현재 자산의 10%씩 투자 (복리)
        "max_pos_usdc":  100.0,       # 단일 포지션 상한
        "reinvest":      True,        # 수익 재투자 (스노우볼)
        "reentry":       True,        # 동일 심볼 방향 재진입 허용
        "traders": [
            "YjCD9Gek6MVY9t3MLEGYYdZLeaF6MZrpgZraayWsv9E",   # CRS 82.5, ROI 113.9%
            "6ZjWoJKeD88JqREHhYAWSZVLQfVcMSbx6eVdajXt9Xbv",  # CRS 82.4, ROI 157.5%
            "4TYEjn9PSpxoBNBXufeuNDRbytzvyyZtEUgXYSk8kYLZ",  # CRS 81.1, ROI 141.7%
        ],
        "expected_30d_roi": 13.7,
    },
    "conservative": {
        "label":        "🛡️ 안정형",
        "copy_ratio":    0.05,        # 보수적: 5%씩
        "max_pos_usdc":  50.0,
        "reinvest":      True,
        "reentry":       False,       # 재진입 없음 (안전 우선)
        "traders": [
            "GNzSLjvyysA4AHEbXq1PgKm9oHqmqZmLdup9vH1z3Z3a",  # 일관성 4/4, 레버 0x
            "BkUTkCt4JwQQwczibKkP5TEjTCHkSogR44ppvQReTt5B",  # 일관성 4/4, 레버 3x
        ],
        "expected_30d_roi": 4.2,
    },
    "balanced": {
        "label":        "⚖️ 균형형",
        "copy_ratio":    0.10,
        "max_pos_usdc":  100.0,
        "reinvest":      True,
        "reentry":       True,
        "traders": [
            "YjCD9Gek6MVY9t3MLEGYYdZLeaF6MZrpgZraayWsv9E",
            "6ZjWoJKeD88JqREHhYAWSZVLQfVcMSbx6eVdajXt9Xbv",
            "4TYEjn9PSpxoBNBXufeuNDRbytzvyyZtEUgXYSk8kYLZ",
            "D5LnbmzTQPCmWBkr9yD2pRq3q5XT4TVmjibhXvsAzj6v",
            "CAHPdCrmxQyt8aGETr6cYedw3QvyqxWBRortR7ddN6bL",
        ],
        "expected_30d_roi": 11.4,
    },
    "aggressive": {
        "label":        "🚀 공격형",
        "copy_ratio":    0.15,        # 15% + 재투자 = 빠른 스노우볼
        "max_pos_usdc":  200.0,
        "reinvest":      True,
        "reentry":       True,        # 적극적 재진입
        "traders": [
            "YjCD9Gek6MVY9t3MLEGYYdZLeaF6MZrpgZraayWsv9E",
            "6ZjWoJKeD88JqREHhYAWSZVLQfVcMSbx6eVdajXt9Xbv",
            "4TYEjn9PSpxoBNBXufeuNDRbytzvyyZtEUgXYSk8kYLZ",
            "Ph9yECGodDAjiiSU9bpbJ8dds3ndWP1ngKo8h1K2QYv",
            "FN4seJZ9Wdi3NCbugCkPD5xYac5UrCQmzQt4o3Ko5VB2",
        ],
        "expected_30d_roi": 23.6,
    },
}

# ── SSL & API ────────────────────────────────────────
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode    = ssl.CERT_NONE
_api_lock = asyncio.Lock() if False else None  # sync 환경
_last_req  = 0.0
_MIN_DELAY = 2.5  # codetabs rate-limit 방지

def _codetabs(path: str, extra: float = 0) -> list | dict | None:
    global _last_req
    gap = time.time() - _last_req
    if gap < _MIN_DELAY + extra:
        time.sleep(_MIN_DELAY + extra - gap)
    target = f"https://api.pacifica.fi/api/v1/{path}"
    proxy  = "https://api.codetabs.com/v1/proxy?quest=" + urllib.parse.quote(target, safe="")
    req    = urllib.request.Request(proxy, headers={"User-Agent": "CopyPerp-4x/1.1"})
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=20) as r:
            raw = r.read()
        _last_req = time.time()
        data = json.loads(raw.decode("utf-8", "ignore"))
        return data.get("data") if isinstance(data, dict) and "data" in data else data
    except Exception as e:
        _last_req = time.time()
        log.debug(f"  codetabs 실패 ({path[:40]}): {e}")
        return None


def get_prices() -> dict[str, float]:
    r = _codetabs("info/prices") or []
    return {p["symbol"]: float(p.get("mark") or p.get("price") or 0) for p in r if p.get("symbol")}


def get_positions(addr: str) -> list:
    return _codetabs(f"positions?account={addr}", extra=1.0) or []


# ── DB 초기화 ────────────────────────────────────────

def init_db(conn: sqlite3.Connection):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS paper_sessions (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy      TEXT UNIQUE,
        started_at    INTEGER,
        last_updated  INTEGER,
        initial_capital REAL,
        current_equity  REAL,
        realized_pnl    REAL,
        unrealized_pnl  REAL,
        total_trades  INTEGER,
        win_trades    INTEGER,
        lose_trades   INTEGER
    );

    CREATE TABLE IF NOT EXISTS paper_trades (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        session_strategy TEXT,
        trader_alias  TEXT,
        symbol        TEXT,
        side          TEXT,
        action        TEXT,
        size          REAL,
        entry_price   REAL,
        exit_price    REAL,
        pnl           REAL,
        roi_pct       REAL,
        hold_min      REAL,
        opened_at     INTEGER,
        closed_at     INTEGER,
        stop_loss_price REAL DEFAULT 0,
        take_profit_price REAL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS paper_snapshots (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy      TEXT,
        snapshot_at   INTEGER,
        equity        REAL,
        realized_pnl  REAL,
        unrealized_pnl REAL,
        open_positions INTEGER,
        total_trades  INTEGER,
        win_rate      REAL
    );

    CREATE TABLE IF NOT EXISTS paper_positions (
        strategy      TEXT,
        symbol        TEXT,
        side          TEXT,
        entry_price   REAL,
        size          REAL,
        usdc_value    REAL,
        trader_addr   TEXT,
        opened_at     INTEGER,
        updated_at    INTEGER,
        PRIMARY KEY (strategy, symbol)
    );
    """)
    conn.commit()


def upsert_session(conn: sqlite3.Connection, strategy: str, capital: float, eq: float,
                   rpnl: float, upnl: float, total: int, wins: int, losses: int):
    now = int(time.time() * 1000)
    conn.execute("""
        INSERT INTO paper_sessions
            (strategy, started_at, last_updated, initial_capital, current_equity,
             realized_pnl, unrealized_pnl, total_trades, win_trades, lose_trades)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(strategy) DO UPDATE SET
            last_updated=excluded.last_updated,
            current_equity=excluded.current_equity,
            realized_pnl=excluded.realized_pnl,
            unrealized_pnl=excluded.unrealized_pnl,
            total_trades=excluded.total_trades,
            win_trades=excluded.win_trades,
            lose_trades=excluded.lose_trades
    """, (strategy, now, now, capital, eq, rpnl, upnl, total, wins, losses))
    conn.commit()


def insert_trade(conn: sqlite3.Connection, strategy: str, trader: str, symbol: str,
                 side: str, action: str, size: float, entry: float, exit_p: float,
                 pnl: float, roi: float, hold_min: float, opened_at: int):
    conn.execute("""
        INSERT INTO paper_trades
            (session_strategy, trader_alias, symbol, side, action, size,
             entry_price, exit_price, pnl, roi_pct, hold_min, opened_at, closed_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (strategy, trader[:16], symbol, side, action, size, entry, exit_p,
          pnl, roi, hold_min, opened_at, int(time.time() * 1000)))
    conn.commit()


def insert_snapshot(conn: sqlite3.Connection, strategy: str, eq: float,
                    rpnl: float, upnl: float, open_pos: int, total: int, wr: float):
    conn.execute("""
        INSERT INTO paper_snapshots
            (strategy, snapshot_at, equity, realized_pnl, unrealized_pnl,
             open_positions, total_trades, win_rate)
        VALUES (?,?,?,?,?,?,?,?)
    """, (strategy, int(time.time() * 1000), eq, rpnl, upnl, open_pos, total, wr))
    conn.commit()


def save_positions(conn: sqlite3.Connection, strategy: str, positions: dict):
    """오픈 포지션을 DB에 저장 (재시작 복구용)"""
    conn.execute("DELETE FROM paper_positions WHERE strategy=?", (strategy,))
    now = int(time.time() * 1000)
    for sym, pos in positions.items():
        conn.execute("""
            INSERT INTO paper_positions
                (strategy, symbol, side, entry_price, size, usdc_value,
                 trader_addr, opened_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (strategy, sym, pos["side"], pos["entry_price"], pos["size"],
              pos["usdc_value"], pos["trader_addr"], pos["opened_at"], now))
    conn.commit()


def load_positions(conn: sqlite3.Connection, strategy: str) -> dict:
    """DB에서 기존 오픈 포지션 복구"""
    cur = conn.cursor()
    cur.execute("SELECT * FROM paper_positions WHERE strategy=?", (strategy,))
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    result = {}
    for r in rows:
        d = dict(zip(cols, r))
        result[d["symbol"]] = {
            "side":        d["side"],
            "entry_price": d["entry_price"],
            "size":        d["size"],
            "usdc_value":  d["usdc_value"],
            "trader_addr": d["trader_addr"],
            "opened_at":   d["opened_at"],
        }
    if result:
        log.info(f"  [{strategy}] 포지션 {len(result)}개 복구됨")
    return result


def load_session_state(conn: sqlite3.Connection, strategy: str) -> Optional[dict]:
    """기존 세션 상태 복구"""
    cur = conn.cursor()
    cur.execute("SELECT * FROM paper_sessions WHERE strategy=?", (strategy,))
    row = cur.fetchone()
    if not row:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


# ── 전략별 엔진 ──────────────────────────────────────

class StrategyEngine:
    def __init__(self, key: str, cfg: dict, capital: float, conn: sqlite3.Connection):
        self.key       = key
        self.label     = cfg["label"]
        self.ratio     = cfg["copy_ratio"]
        self.max_pos   = cfg["max_pos_usdc"]
        self.reinvest  = cfg.get("reinvest", True)   # 수익 재투자
        self.reentry   = cfg.get("reentry", True)    # 동일 심볼 재진입
        self.traders   = cfg["traders"]
        self.exp_roi   = cfg["expected_30d_roi"]
        self.conn      = conn
        self.is_warmup = True   # 첫 사이클: prev 기록만, 진입 없음

        # 기존 세션 복구 시도
        old = load_session_state(conn, key)
        if old and old["total_trades"] > 0:
            self.cash      = old["current_equity"] - 0  # 포지션 제외
            self.realized  = old["realized_pnl"]
            self.wins      = old["win_trades"]
            self.losses    = old["lose_trades"]
            self.initial   = old["initial_capital"]
            self.positions = load_positions(conn, key)
            log.info(f"  [{key}] 기존 세션 복구: PnL={self.realized:+.2f}, 거래={old['total_trades']}건")
        else:
            self.cash      = capital
            self.realized  = 0.0
            self.wins      = 0
            self.losses    = 0
            self.initial   = capital
            self.positions = {}
            # 새 세션 생성
            upsert_session(conn, key, capital, capital, 0, 0, 0, 0, 0)
            log.info(f"  [{key}] 신규 세션 시작: ${capital:,.0f}")

        self.prev_positions: dict[str, dict[str, str]] = {}  # trader_addr → {symbol: side}
        self.peak_eq = self.cash
        self.prices  = {}

    def equity(self) -> float:
        upnl = sum(self._upnl(sym, pos) for sym, pos in self.positions.items())
        return self.cash + upnl

    def unrealized_pnl(self) -> float:
        return sum(self._upnl(sym, pos) for sym, pos in self.positions.items())

    def _upnl(self, symbol: str, pos: dict) -> float:
        cur = self.prices.get(symbol, pos["entry_price"])
        diff = cur - pos["entry_price"]
        if pos["side"] == "short":
            diff = -diff
        return diff * pos["size"]

    def win_rate(self) -> float:
        total = self.wins + self.losses
        return self.wins / total * 100 if total else 0.0

    def roi(self) -> float:
        return (self.equity() - self.initial) / self.initial * 100

    def open_pos(self, symbol: str, side: str, trader: str) -> bool:
        # 웜업 중이면 진입 금지 (첫 사이클은 prev 기록만)
        if self.is_warmup:
            return False

        if symbol in self.positions:
            existing = self.positions[symbol]
            if existing["side"] == side:
                # 동일 방향 재진입: reentry 허용 시 청산 후 재진입 (스노우볼)
                if self.reentry:
                    self.close_pos(symbol, "재진입")
                else:
                    return False  # 안정형: 재진입 금지
            else:
                self.close_pos(symbol, "반전")

        price = self.prices.get(symbol, 0)
        if not price:
            return False

        slip       = price * SLIPPAGE_BPS / 10000
        exec_price = price + slip if side == "long" else price - slip

        # 스노우볼: 투자금 = 현재 자산 × copy_ratio (복리 효과)
        current_equity = self.equity()
        base_usdc = current_equity * self.ratio if self.reinvest else self.initial * self.ratio
        usdc = min(base_usdc, self.max_pos)

        # 현금 최소 보유 (전체 자산의 20% 이상 현금 유지)
        min_cash = current_equity * 0.20
        if self.cash - usdc < min_cash:
            usdc = max(0, self.cash - min_cash)

        if usdc < 3:
            return False

        size = usdc / exec_price
        fee  = usdc * TAKER_FEE

        self.cash -= (usdc + fee)
        self.realized -= fee  # 수수료는 비용

        opened_at = int(time.time() * 1000)
        self.positions[symbol] = {
            "side":        side,
            "entry_price": exec_price,
            "size":        size,
            "usdc_value":  usdc,
            "trader_addr": trader,
            "opened_at":   opened_at,
        }

        arrow = "📈" if side == "long" else "📉"
        log.info(f"    {arrow} [{self.key}] OPEN {symbol} {side.upper()} ${usdc:,.0f} @{exec_price:.4f}")

        insert_trade(self.conn, self.key, trader, symbol, side, "open",
                     size, exec_price, 0, 0, 0, 0, opened_at)
        save_positions(self.conn, self.key, self.positions)
        return True

    def close_pos(self, symbol: str, reason: str = "") -> float:
        pos = self.positions.get(symbol)
        if not pos:
            return 0.0

        price      = self.prices.get(symbol, pos["entry_price"])
        slip       = price * SLIPPAGE_BPS / 10000
        exec_price = price - slip if pos["side"] == "long" else price + slip

        upnl = self._upnl(symbol, pos)
        fee  = pos["usdc_value"] * TAKER_FEE
        pnl  = upnl - fee

        self.cash      += pos["usdc_value"] + upnl - fee
        self.realized  += pnl

        if pnl > 0:
            self.wins   += 1
        else:
            self.losses += 1

        hold_ms  = int(time.time() * 1000) - pos["opened_at"]
        hold_min = hold_ms / 60000
        roi_pct  = pnl / pos["usdc_value"] * 100 if pos["usdc_value"] else 0

        emoji = "✅" if pnl > 0 else "❌"
        log.info(f"    {emoji} [{self.key}] CLOSE {symbol} {pos['side'].upper()} PnL={pnl:+.2f} ({reason})")

        insert_trade(self.conn, self.key, pos["trader_addr"], symbol, pos["side"], "close",
                     pos["size"], pos["entry_price"], exec_price, pnl, roi_pct, hold_min, pos["opened_at"])

        del self.positions[symbol]
        save_positions(self.conn, self.key, self.positions)
        return pnl

    def sync_trader(self, addr: str) -> int:
        pos_list = get_positions(addr)
        if pos_list is None:
            return 0

        current: dict[str, str] = {}
        for p in pos_list:
            sym  = p.get("symbol", "")
            raw  = p.get("side", "")
            side = "long" if raw == "bid" else ("short" if raw == "ask" else "")
            if sym and side:
                current[sym] = side

        prev    = self.prev_positions.get(addr, {})
        changes = 0

        for sym, side in current.items():
            if sym not in prev:
                if self.open_pos(sym, side, addr):
                    changes += 1
            elif prev[sym] != side:
                if self.open_pos(sym, side, addr):  # 반전
                    changes += 1

        for sym in list(prev.keys()):
            if sym not in current and sym in self.positions:
                self.close_pos(sym, "트레이더 청산")
                changes += 1

        self.prev_positions[addr] = current
        return changes

    def flush_db(self):
        """현재 상태를 DB에 동기화"""
        eq    = self.equity()
        upnl  = self.unrealized_pnl()
        total = self.wins + self.losses
        wr    = self.win_rate()

        if eq > self.peak_eq:
            self.peak_eq = eq

        upsert_session(self.conn, self.key, self.initial, eq,
                       self.realized, upnl, total, self.wins, self.losses)
        insert_snapshot(self.conn, self.key, eq, self.realized, upnl,
                        len(self.positions), total, wr)

    def summary(self) -> dict:
        eq   = self.equity()
        upnl = self.unrealized_pnl()
        dd   = max(0, (self.peak_eq - eq) / self.peak_eq * 100) if self.peak_eq else 0
        return {
            "strategy":       self.key,
            "label":          self.label,
            "initial":        self.initial,
            "equity":         round(eq, 4),
            "realized_pnl":   round(self.realized, 4),
            "unrealized_pnl": round(upnl, 4),
            "total_pnl":      round(self.realized + upnl, 4),
            "roi_pct":        round(self.roi(), 4),
            "drawdown_pct":   round(dd, 4),
            "win_rate":       round(self.win_rate(), 2),
            "wins":           self.wins,
            "losses":         self.losses,
            "total_trades":   self.wins + self.losses,
            "open_positions": len(self.positions),
            "n_traders":      len(self.traders),
            "expected_30d":   self.exp_roi,
        }

    def print_summary(self):
        s = self.summary()
        roi_emoji = "🟢" if s["roi_pct"] >= 0 else "🔴"
        log.info(
            f"  {self.label:10} | eq=${s['equity']:,.2f} {roi_emoji} ROI={s['roi_pct']:+.2f}% "
            f"| PnL=${s['realized_pnl']:+.2f} | uPnL=${s['unrealized_pnl']:+.2f} "
            f"| 포지션={s['open_positions']} | 거래={s['total_trades']}건"
        )


# ── 메인 루프 ────────────────────────────────────────

def run_4x(capital: float = 10_000, interval: int = 60):
    db_path = os.path.join(ROOT, "paper_perp.db")
    conn    = sqlite3.connect(db_path, check_same_thread=False)
    init_db(conn)

    log.info("=" * 70)
    log.info("Copy Perp — 4x Strategy Paper Trading")
    log.info(f"  자본: ${capital:,.0f}  |  폴링: {interval}초  |  DB: {db_path}")
    log.info("=" * 70)

    # 엔진 초기화
    engines = {
        key: StrategyEngine(key, cfg, capital, conn)
        for key, cfg in STRATEGIES.items()
    }

    cycle    = 0
    last_snap = 0
    SNAP_INT = 300  # 5분마다 스냅샷

    log.info("\n🚀 4개 전략 병렬 페이퍼트레이딩 시작!\n")

    while True:
        cycle += 1
        now   = time.time()
        ts    = datetime.now().strftime("%H:%M:%S")
        log.info(f"\n{'─'*70}")
        log.info(f"사이클 #{cycle:4d}  |  {ts}  |  4x Paper Trading")

        # ── 가격 업데이트 ─────────────────────────
        try:
            prices = get_prices()
            if prices:
                btc = prices.get("BTC", 0)
                eth = prices.get("ETH", 0)
                sol = prices.get("SOL", 0)
                log.info(f"  💹 {len(prices)}개 심볼  BTC=${btc:,.0f}  ETH=${eth:,.2f}  SOL=${sol:,.2f}")
                for eng in engines.values():
                    eng.prices = prices
            else:
                log.warning("  가격 조회 실패 — 이전 가격 유지")
        except Exception as e:
            log.warning(f"  가격 업데이트 오류: {e}")

        # ── 트레이더 포지션 동기화 (전략별, 트레이더별) ──
        unique_traders: dict[str, list] = {}  # addr → [engine_key, ...]
        for key, eng in engines.items():
            for addr in eng.traders:
                unique_traders.setdefault(addr, []).append(key)

        # 중복 주소 한 번만 조회
        fetched_positions: dict[str, list] = {}
        for addr in unique_traders:
            positions = get_positions(addr)
            if positions is not None:
                fetched_positions[addr] = positions
                log.debug(f"  조회 {addr[:14]}... {len(positions)}개 포지션")

        # 각 엔진에 적용
        total_changes = 0
        for key, eng in engines.items():
            for addr in eng.traders:
                if addr not in fetched_positions:
                    continue
                pos_list = fetched_positions[addr]

                current: dict[str, str] = {}
                for p in pos_list:
                    sym  = p.get("symbol", "")
                    raw  = p.get("side", "")
                    side = "long" if raw == "bid" else ("short" if raw == "ask" else "")
                    if sym and side:
                        current[sym] = side

                prev = eng.prev_positions.get(addr, {})
                for sym, side in current.items():
                    if sym not in prev:
                        if eng.open_pos(sym, side, addr):
                            total_changes += 1
                    elif prev[sym] != side:
                        if eng.open_pos(sym, side, addr):
                            total_changes += 1
                for sym in list(prev.keys()):
                    if sym not in current and sym in eng.positions:
                        eng.close_pos(sym, "트레이더 청산")
                        total_changes += 1
                eng.prev_positions[addr] = current

        # 웜업 해제 (첫 사이클 완료 후)
        for eng in engines.values():
            if eng.is_warmup:
                eng.is_warmup = False
                log.info(f"  [{eng.key}] 웜업 완료 → 다음 사이클부터 실제 진입")

        if total_changes == 0:
            log.info(f"  → 포지션 변화 없음 (총 {len(unique_traders)}명 감시 중)")
        else:
            log.info(f"  → {total_changes}건 변화 감지")

        # ── 현황 출력 + DB flush ──────────────────
        log.info(f"\n  📊 전략별 현황:")
        for eng in engines.values():
            eng.flush_db()
            eng.print_summary()

        # ── 5분마다 스냅샷 강제 ──────────────────
        if now - last_snap >= SNAP_INT:
            log.info(f"\n  💾 스냅샷 저장 ({SNAP_INT//60}분 주기)")
            last_snap = now

        log.info(f"\n  ⏰ {interval}초 대기...")
        time.sleep(interval)


# ── 초기화 전용: 세션 리셋 ──────────────────────────

def reset_sessions(capital: float = 10_000):
    db_path = os.path.join(ROOT, "paper_perp.db")
    conn = sqlite3.connect(db_path)
    init_db(conn)
    conn.execute("DELETE FROM paper_sessions")
    conn.execute("DELETE FROM paper_positions")
    conn.commit()
    now = int(time.time() * 1000)
    for key in STRATEGIES:
        conn.execute("""
            INSERT INTO paper_sessions
                (strategy, started_at, last_updated, initial_capital, current_equity,
                 realized_pnl, unrealized_pnl, total_trades, win_trades, lose_trades)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (key, now, now, capital, capital, 0, 0, 0, 0, 0))
    conn.commit()
    conn.close()
    print(f"✅ 4개 전략 세션 초기화 완료 (초기 자본: ${capital:,.0f})")


# ── CLI ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="4x Strategy Paper Trading")
    parser.add_argument("--interval", type=int,   default=60,     help="폴링 간격 (초)")
    parser.add_argument("--capital",  type=float, default=10_000, help="초기 자본 USDC")
    parser.add_argument("--reset",    action="store_true",         help="세션 초기화 후 시작")
    args = parser.parse_args()

    if args.reset:
        reset_sessions(args.capital)

    try:
        run_4x(capital=args.capital, interval=args.interval)
    except KeyboardInterrupt:
        log.info("\n🛑 종료됨")
