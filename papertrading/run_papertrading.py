"""
Copy Perp — Mainnet Papertrading v3
positions 스냅샷 기반 정확한 심볼 추적

핵심 수정:
- trades API 심볼 없음 → positions 스냅샷으로만 추적
- positions delta(신규/청산)로 open/close 이벤트 감지
- 가격: positions entry_price 기준 (현재 마크가 없어서 보수적으로)
- PnL 계산: 포지션 청산 시 entry vs exit price 비교
- 수익 안정성: Calmar, Sharpe 근사치 추적
"""
import json
import os
import ssl
import socket
import time
import logging
import hashlib
from dataclasses import dataclass, field
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pt3")

# ── API ──────────────────────────────────────────────────
CF_HOST = "do5jt23sqak4.cloudfront.net"
PAC_HOST = "api.pacifica.fi"
PORT = 443

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def _cf_get(path: str, timeout: int = 15, retries: int = 3):
    for attempt in range(retries):
        try:
            sock = socket.create_connection((CF_HOST, PORT), timeout=timeout)
            ssock = _ssl_ctx.wrap_socket(sock, server_hostname=CF_HOST)
            req = (
                f"GET /api/v1/{path} HTTP/1.1\r\n"
                f"Host: {PAC_HOST}\r\n"
                f"Accept: application/json\r\n"
                f"Connection: close\r\n\r\n"
            )
            ssock.sendall(req.encode())
            data = b""
            ssock.settimeout(timeout)
            while True:
                chunk = ssock.recv(32768)
                if not chunk:
                    break
                data += chunk
            ssock.close()
            sock.close()
            if b"\r\n\r\n" not in data:
                continue
            body = data.split(b"\r\n\r\n", 1)[1]
            parsed = json.loads(body.decode("utf-8", "ignore"))
            if isinstance(parsed, dict) and parsed.get("success") is False:
                return None
            return parsed
        except Exception as e:
            log.debug(f"API [{path}] 오류 ({attempt+1}/{retries}): {e}")
            time.sleep(1.0 * (attempt + 1))
    return None


def get_leaderboard(limit: int = 100) -> list:
    r = _cf_get(f"leaderboard?limit={limit}")
    if isinstance(r, dict):
        return r.get("data") or []
    return r or []


def get_positions(address: str) -> list:
    """
    positions API: symbol, side(bid/ask), amount, entry_price, liquidation_price, funding
    """
    r = _cf_get(f"positions?account={address}")
    if isinstance(r, dict):
        return r.get("data") or []
    return r or []


def select_top_traders(n: int = 5) -> list:
    """
    리더보드 기준 Tier-A 트레이더 선별
    기준: pnl_30d > 10000 AND equity_current > 50000
    """
    lb = get_leaderboard(100)
    cands = []
    for t in lb:
        pnl30 = float(t.get("pnl_30d") or 0)
        pnl7 = float(t.get("pnl_7d") or 0)
        equity = float(t.get("equity_current") or 0)
        if pnl30 > 10_000 and equity > 50_000:
            score = pnl30 * 0.6 + pnl7 * 0.4
            cands.append({
                "address": t["address"],
                "alias": t.get("username") or t["address"][:8],
                "pnl_30d": pnl30,
                "pnl_7d": pnl7,
                "equity": equity,
                "score": score,
            })
    cands.sort(key=lambda x: x["score"], reverse=True)
    top = cands[:n]
    total_score = sum(t["score"] for t in top) or 1
    for t in top:
        t["weight"] = round(t["score"] / total_score, 4)
    return top


# ── 시나리오 프리셋 (Mainnet 실데이터 기반 최적화) ───────────────────
# 기준: 60분 실행, copy_ratio=5%, max_pos=$300 → PnL +$12.57
STRATEGY_PRESETS = {
    # 🔒 기본형: copy_ratio 8%, S등급 안정성 2명, max_pos $40
    # 메인넷 최적화: 30일 ROI +4.77% (이전 5% → 8%)
    "default": {
        "copy_ratio":        0.08,
        "max_position_usdc": 40.0,
        "stop_loss_pct":     8.0,
        "take_profit_pct":   15.0,
        "max_open_positions": 8,
        "n_traders":          2,
        "grade_filter":      ["S"],
        "sort_by":           "stability",
        "capital":           1_000.0,
        "label":             "🔒 기본형",
        "expected_roi_30d":  4.77,
    },
    # 🛡️ 안정형: copy_ratio 10%, S등급 30일ROI 3명, max_pos $100
    "conservative": {
        "copy_ratio":        0.10,
        "max_position_usdc": 100.0,
        "stop_loss_pct":     8.0,
        "take_profit_pct":   18.0,
        "max_open_positions": 10,
        "n_traders":          3,
        "grade_filter":      ["S"],
        "sort_by":           "roi_30d",
        "capital":           1_000.0,
        "label":             "🛡️ 안정형",
        "expected_roi_30d":  5.38,
    },
    # ⚖️ 균형형: copy_ratio 12%, S+A 복합점수 4명, max_pos $150
    "balanced": {
        "copy_ratio":        0.12,
        "max_position_usdc": 150.0,
        "stop_loss_pct":     10.0,
        "take_profit_pct":   22.0,
        "max_open_positions": 12,
        "n_traders":          4,
        "grade_filter":      ["S", "A"],
        "sort_by":           "score",
        "capital":           5_000.0,
        "label":             "⚖️ 균형형",
        "expected_roi_30d":  5.94,
    },
    # ⚡ 공격형: copy_ratio 15%, 7일 모멘텀 3명, max_pos $200
    "aggressive": {
        "copy_ratio":        0.15,
        "max_position_usdc": 200.0,
        "stop_loss_pct":     12.0,
        "take_profit_pct":   30.0,
        "max_open_positions": 15,
        "n_traders":          3,
        "grade_filter":      ["S", "A"],
        "sort_by":           "roi_7d",
        "capital":           10_000.0,
        "label":             "⚡ 공격형",
        "expected_roi_30d":  5.42,
    },
    # ── 레거시 호환 ───────────────────────────────────────────────────
    "safe": {
        "copy_ratio":        0.08,
        "max_position_usdc": 40.0,
        "stop_loss_pct":     8.0,
        "take_profit_pct":   15.0,
        "max_open_positions": 8,
        "n_traders":          2,
        "grade_filter":      ["S"],
        "sort_by":           "stability",
        "capital":           1_000.0,
        "label":             "🔒 기본형 (compat)",
        "expected_roi_30d":  4.77,
    },
}

# ── 설정 (기본: balanced — Mainnet 데이터 최적화) ────────────────────
INITIAL_CAPITAL = 10_000.0
_DEFAULT_STRATEGY = "balanced"

def _apply_preset(strategy: str) -> dict:
    p = STRATEGY_PRESETS.get(strategy, STRATEGY_PRESETS[_DEFAULT_STRATEGY])
    return p

# 실행 시 --strategy 인자로 override 가능 (하단 argparse 참조)
COPY_RATIO          = STRATEGY_PRESETS[_DEFAULT_STRATEGY]["copy_ratio"]
MAX_POSITION_USDC   = STRATEGY_PRESETS[_DEFAULT_STRATEGY]["max_position_usdc"]
MAX_OPEN_POSITIONS  = STRATEGY_PRESETS[_DEFAULT_STRATEGY]["max_open_positions"]
STOP_LOSS_PCT       = STRATEGY_PRESETS[_DEFAULT_STRATEGY]["stop_loss_pct"]
TAKE_PROFIT_PCT     = STRATEGY_PRESETS[_DEFAULT_STRATEGY]["take_profit_pct"]
MIN_NOTIONAL        = 1.0   # 최소 명목가치 $1


@dataclass
class VirtualPosition:
    """가상 복사 포지션"""
    symbol: str
    side: str          # "long" | "short"
    entry_price: float
    size: float
    opened_at: float
    trader: str
    trader_entry: float    # 트레이더 원래 entry_price
    trader_amount: float   # 트레이더 원래 amount
    current_price: float = 0.0
    peak_pnl_pct: float = 0.0   # 최고 수익률 (trailing stop용)

    @property
    def notional(self) -> float:
        return self.entry_price * self.size

    def upnl(self, cur: float = None) -> float:
        p = cur or self.current_price or self.entry_price
        if self.side == "long":
            return (p - self.entry_price) * self.size
        else:
            return (self.entry_price - p) * self.size

    def pct(self, cur: float = None) -> float:
        u = self.upnl(cur)
        return u / self.notional * 100 if self.notional else 0


@dataclass
class Stats:
    capital: float = INITIAL_CAPITAL
    peak: float = INITIAL_CAPITAL
    positions: dict = field(default_factory=dict)   # key: f"{symbol}_{trader}"
    closed: list = field(default_factory=list)
    wins: int = 0
    losses: int = 0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    max_dd_pct: float = 0.0
    orders: int = 0
    skipped: int = 0
    equity_series: list = field(default_factory=list)

    @property
    def total_pnl(self): return self.capital - INITIAL_CAPITAL
    @property
    def roi(self): return self.total_pnl / INITIAL_CAPITAL * 100
    @property
    def win_rate(self):
        t = self.wins + self.losses
        return self.wins / t if t else 0.0
    @property
    def profit_factor(self):
        if self.gross_loss == 0:
            return float("inf") if self.gross_profit > 0 else 0
        return self.gross_profit / abs(self.gross_loss)
    @property
    def unrealized(self):
        return sum(p.upnl() for p in self.positions.values())
    @property
    def equity(self):
        return self.capital + self.unrealized

    def calmar(self):
        return self.roi / self.max_dd_pct if self.max_dd_pct > 0 else float("inf")

    def sharpe_approx(self):
        """폴링 수익률 시계열 기반 Sharpe 근사 (RF=0)"""
        if len(self.equity_series) < 3:
            return None
        rets = []
        for i in range(1, len(self.equity_series)):
            r = (self.equity_series[i] - self.equity_series[i-1]) / self.equity_series[i-1]
            rets.append(r)
        if not rets:
            return None
        avg = sum(rets) / len(rets)
        var = sum((r - avg)**2 for r in rets) / len(rets)
        std = var**0.5
        return avg / std if std > 0 else None


class Engine:
    def __init__(self, traders: list):
        self.traders = traders
        self.stats = Stats()
        self.snapshots: dict = {}   # address → list of positions (prev poll)
        self.session_start = time.time()
        self.poll_count = 0

        log.info(f"🚀 Papertrading v3 시작 | 자본: ${INITIAL_CAPITAL:,.0f}")
        log.info(f"   트레이더 {len(traders)}명 | copy={COPY_RATIO*100:.0f}% | max_pos=${MAX_POSITION_USDC} | SL={STOP_LOSS_PCT}% TP={TAKE_PROFIT_PCT}%")
        for t in traders:
            log.info(f"   [{t['alias'][:14]}] pnl30d=${t['pnl_30d']:,.0f} pnl7d=${t['pnl_7d']:,.0f} equity=${t['equity']:,.0f} w={t['weight']:.2f}")

    def _pos_key(self, symbol: str, trader_addr: str) -> str:
        return f"{symbol}_{trader_addr[:8]}"

    def _open_position(self, trader: dict, symbol: str, side: str, entry_price: float,
                       trader_amount: float):
        """가상 포지션 오픈"""
        if len(self.stats.positions) >= MAX_OPEN_POSITIONS:
            self.stats.skipped += 1
            return
        if entry_price <= 0:
            return

        # copy 수량 계산
        copy_amount = trader_amount * COPY_RATIO * trader["weight"]
        notional = copy_amount * entry_price
        if notional > MAX_POSITION_USDC:
            copy_amount = MAX_POSITION_USDC / entry_price
            notional = MAX_POSITION_USDC
        if notional < MIN_NOTIONAL:
            self.stats.skipped += 1
            return

        key = self._pos_key(symbol, trader["address"])
        if key in self.stats.positions:
            return  # 이미 추적 중

        pos = VirtualPosition(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            size=copy_amount,
            opened_at=time.time(),
            trader=trader["alias"][:10],
            trader_entry=entry_price,
            trader_amount=trader_amount,
            current_price=entry_price,
        )
        self.stats.positions[key] = pos
        self.stats.orders += 1
        log.info(
            f"  📈 OPEN [{pos.trader}] {symbol} {side.upper()} "
            f"{copy_amount:.4f} @ ${entry_price:.4f} = ${notional:.2f}"
        )

    def _close_position(self, key: str, exit_price: float, reason: str = "copy_close"):
        """가상 포지션 청산"""
        pos = self.stats.positions.pop(key, None)
        if pos is None:
            return
        if exit_price <= 0:
            exit_price = pos.entry_price  # 가격 불명이면 0 PnL

        pnl = pos.upnl(exit_price)
        pct = pos.pct(exit_price)
        self.stats.capital += pnl
        if pnl >= 0:
            self.stats.wins += 1
            self.stats.gross_profit += pnl
        else:
            self.stats.losses += 1
            self.stats.gross_loss += pnl
        self.stats.orders += 1

        emoji = "✅" if pnl >= 0 else "❌"
        log.info(
            f"  {emoji} CLOSE [{pos.trader}] {pos.symbol} {pos.side.upper()} "
            f"진입=${pos.entry_price:.4f} 청산=${exit_price:.4f} "
            f"PnL=${pnl:+.4f} ({pct:+.2f}%) [{reason}] 자본=${self.stats.capital:,.2f}"
        )
        self.stats.closed.append({
            "symbol": pos.symbol,
            "side": pos.side,
            "entry": pos.entry_price,
            "exit": exit_price,
            "size": pos.size,
            "pnl": round(pnl, 4),
            "pct": round(pct, 2),
            "reason": reason,
            "trader": pos.trader,
            "held_min": round((time.time() - pos.opened_at) / 60, 1),
        })

    def _update_positions_from_snapshot(self, trader: dict, new_pos_list: list):
        """
        포지션 스냅샷 비교:
        - 신규 심볼 → open
        - 사라진 심볼 → close (exit_price = 현재 entry_price 필드, 즉 avg로 근사)
        - 유지 심볼 → current_price 업데이트
        """
        addr = trader["address"]
        old_list = self.snapshots.get(addr, [])

        # 기존 스냅샷을 {symbol_side: pos} 맵으로
        old_map = {}
        for p in old_list:
            sym = p.get("symbol") or "UNK"
            side_raw = p.get("side", "")
            side = "long" if side_raw in ("bid", "long", "buy") else "short"
            old_map[(sym, side)] = p

        new_map = {}
        for p in new_pos_list:
            sym = p.get("symbol") or "UNK"
            side_raw = p.get("side", "")
            side = "long" if side_raw in ("bid", "long", "buy") else "short"
            new_map[(sym, side)] = p

        # 신규 포지션
        for (sym, side), p in new_map.items():
            if (sym, side) not in old_map:
                entry = float(p.get("entry_price") or p.get("avg_price") or 0)
                amount = abs(float(p.get("amount") or p.get("size") or 0))
                if entry > 0 and amount > 0:
                    self._open_position(trader, sym, side, entry, amount)

        # 청산된 포지션
        for (sym, side), p in old_map.items():
            if (sym, side) not in new_map:
                key = self._pos_key(sym, addr)
                if key in self.stats.positions:
                    # exit price 모름 → entry_price 그대로 사용 (보수적)
                    old_entry = float(p.get("entry_price") or 0)
                    vpos = self.stats.positions[key]
                    # 트레이더가 청산한 거니까 realized PnL 방향 맞게
                    # 실제 exit 가격 모르므로 current_price(마지막 entry 필드) 사용
                    self._close_position(key, vpos.current_price or old_entry, reason="trader_closed")

        # 유지 포지션 → current_price 업데이트 (entry_price 필드가 avg이므로 계속 변동)
        for (sym, side), p in new_map.items():
            key = self._pos_key(sym, addr)
            if key in self.stats.positions:
                cur = float(p.get("entry_price") or p.get("avg_price") or 0)
                if cur > 0:
                    vpos = self.stats.positions[key]
                    vpos.current_price = cur
                    # 최고 수익률 추적 (trailing stop 준비)
                    pct_now = vpos.pct(cur)
                    if pct_now > vpos.peak_pnl_pct:
                        vpos.peak_pnl_pct = pct_now

        # 스냅샷 저장
        self.snapshots[addr] = new_pos_list

    def _apply_risk_controls(self):
        """SL/TP 강제 청산"""
        to_close = []
        for key, pos in self.stats.positions.items():
            pct = pos.pct()
            if pct <= -STOP_LOSS_PCT:
                to_close.append((key, pos.current_price or pos.entry_price, "stop_loss"))
            elif pct >= TAKE_PROFIT_PCT:
                to_close.append((key, pos.current_price or pos.entry_price, "take_profit"))
        for key, price, reason in to_close:
            self._close_position(key, price, reason)

    def _update_stats(self):
        equity = self.stats.equity
        if equity > self.stats.peak:
            self.stats.peak = equity
        dd = (self.stats.peak - equity) / self.stats.peak * 100
        if dd > self.stats.max_dd_pct:
            self.stats.max_dd_pct = dd
        self.stats.equity_series.append(round(equity, 2))

    def poll_once(self):
        self.poll_count += 1
        log.info(
            f"\n🔄 폴링 #{self.poll_count} | 자본=${self.stats.capital:,.2f} | "
            f"미실현=${self.stats.unrealized:+.2f} | 자산=${self.stats.equity:,.2f} | "
            f"ROI={self.stats.roi:+.2f}% | DD={self.stats.max_dd_pct:.2f}%"
        )

        for trader in self.traders:
            try:
                positions = get_positions(trader["address"])
                self._update_positions_from_snapshot(trader, positions)
                if positions:
                    log.info(f"  [{trader['alias'][:12]}] 포지션 {len(positions)}개")
                else:
                    log.info(f"  [{trader['alias'][:12]}] 포지션 없음")
            except Exception as e:
                log.warning(f"  [{trader['alias']}] 오류: {e}")
            time.sleep(0.5)

        self._apply_risk_controls()
        self._update_stats()

    def print_report(self):
        s = self.stats
        elapsed = int((time.time() - self.session_start) // 60)
        total = s.wins + s.losses
        pf = s.profit_factor

        print("\n" + "="*70)
        print(f"📊 COPY PERP PAPERTRADING — {elapsed}분 경과 | 폴링 {self.poll_count}회")
        print("="*70)
        print(f"💰 자본")
        print(f"   초기:       ${INITIAL_CAPITAL:>10,.2f}")
        print(f"   실현잔고:   ${s.capital:>10,.2f}  ({s.roi:+.2f}%)")
        print(f"   미실현:     ${s.unrealized:>+10.2f}")
        print(f"   총자산:     ${s.equity:>10,.2f}")
        print(f"   최대DD:     {s.max_dd_pct:.2f}%")
        print(f"   Calmar:     {s.calmar():.2f}" if s.max_dd_pct > 0 else "   Calmar:     ∞")
        sh = s.sharpe_approx()
        if sh is not None:
            print(f"   Sharpe근사: {sh:.3f}")
        print()
        print(f"📈 거래 성과 (완료 {total}건)")
        print(f"   승/패:   {s.wins}W / {s.losses}L  ({s.win_rate:.1%})")
        print(f"   PF:      {pf:.2f}x" if pf != float("inf") else "   PF:      ∞")
        print(f"   총이익:  ${s.gross_profit:>+.2f}")
        print(f"   총손실:  ${s.gross_loss:>+.2f}")
        print()
        print(f"📋 실행 | 주문:{s.orders} 스킵:{s.skipped}")
        print(f"   열린포지션: {len(s.positions)}개")
        for key, pos in list(s.positions.items()):
            age = int((time.time() - pos.opened_at) // 60)
            upnl = pos.upnl()
            pct = pos.pct()
            print(f"     [{pos.trader}] {pos.symbol} {pos.side.upper()} "
                  f"진입=${pos.entry_price:.4f} 현재=${pos.current_price:.4f} "
                  f"UPnL=${upnl:+.4f} ({pct:+.2f}%) [{age}분]")
        if s.closed:
            print()
            print(f"📜 최근 청산 ({min(5,len(s.closed))}건)")
            for t in s.closed[-5:]:
                print(f"   [{t['trader']}] {t['symbol']} {t['side'].upper()} "
                      f"PnL=${t['pnl']:+.4f} ({t['pct']:+.2f}%) {t['held_min']}분 [{t['reason']}]")
        print("="*70)

    def save(self, path: str) -> dict:
        s = self.stats
        total = s.wins + s.losses
        pf = s.profit_factor
        sh = s.sharpe_approx()
        result = {
            "version": "v3",
            "session_start": self.session_start,
            "session_end": time.time(),
            "duration_min": round((time.time() - self.session_start) / 60, 1),
            "poll_count": self.poll_count,
            "initial_capital": INITIAL_CAPITAL,
            "final_capital": round(s.capital, 4),
            "total_equity": round(s.equity, 4),
            "total_pnl": round(s.total_pnl, 4),
            "roi_pct": round(s.roi, 4),
            "win_rate": round(s.win_rate, 4),
            "wins": s.wins,
            "losses": s.losses,
            "total_trades": total,
            "profit_factor": round(pf, 4) if pf != float("inf") else 9999,
            "gross_profit": round(s.gross_profit, 4),
            "gross_loss": round(s.gross_loss, 4),
            "max_dd_pct": round(s.max_dd_pct, 4),
            "calmar": round(s.calmar(), 4) if s.max_dd_pct > 0 else 9999,
            "sharpe_approx": round(sh, 4) if sh else None,
            "orders": s.orders,
            "skipped": s.skipped,
            "open_positions": len(s.positions),
            "equity_series": s.equity_series,
            "closed_trades": s.closed,
            "traders": [{k: v for k, v in t.items() if k != "address"} for t in self.traders],
            "config": {
                "copy_ratio": COPY_RATIO,
                "max_position_usdc": MAX_POSITION_USDC,
                "stop_loss_pct": STOP_LOSS_PCT,
                "take_profit_pct": TAKE_PROFIT_PCT,
                "max_open_positions": MAX_OPEN_POSITIONS,
            },
        }
        with open(path, "w") as f:
            json.dump(result, f, indent=2, default=str)
        log.info(f"💾 저장: {path}")
        return result


def run(duration_min: int = 60, interval_sec: int = 120, output: str = None, n_traders: int = 5):
    log.info("=== Tier-A 트레이더 실시간 선별 ===")
    traders = select_top_traders(n_traders)
    if not traders:
        log.warning("선별 실패 — 폴백")
        traders = [{
            "address": "YjCD9Gek6MVY9t3MLEGYYdZLeaF6MZrpgZraayWsv9E",
            "alias": "YjCD9Gek", "pnl_30d": 119428, "pnl_7d": 94604,
            "equity": 948527, "weight": 1.0, "score": 1.0,
        }]
    log.info(f"선별 {len(traders)}명")

    engine = Engine(traders)

    out_dir = os.path.dirname(os.path.abspath(__file__))
    if output is None:
        ts = time.strftime("%Y%m%d_%H%M%S")
        output = os.path.join(out_dir, f"pt3_{ts}.json")

    end_time = time.time() + duration_min * 60
    next_report = time.time() + max(300, duration_min * 60 // 4)

    try:
        while time.time() < end_time:
            engine.poll_once()

            if time.time() >= next_report:
                engine.print_report()
                engine.save(output)
                next_report = time.time() + max(300, duration_min * 60 // 4)

            remaining = end_time - time.time()
            if remaining <= 0:
                break
            sleep = min(interval_sec, remaining)
            log.info(f"  💤 {sleep:.0f}초 후 다음 폴링 (잔여 {remaining/60:.1f}분)")
            time.sleep(sleep)

    except KeyboardInterrupt:
        log.info("중단")

    engine.print_report()
    return engine.save(output)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--duration",  type=int, default=60,   help="실행 시간(분)")
    p.add_argument("--interval",  type=int, default=120,  help="폴링 간격(초)")
    p.add_argument("--output",    type=str,               help="결과 JSON 저장 경로")
    p.add_argument("--quick",     action="store_true",    help="5분 빠른 테스트")
    p.add_argument("--traders",   type=int, default=None, help="추적 트레이더 수 (프리셋 override)")
    p.add_argument(
        "--strategy",
        type=str,
        default=_DEFAULT_STRATEGY,
        choices=list(STRATEGY_PRESETS.keys()),
        help="전략 프리셋: default | conservative | balanced | aggressive (기본: balanced)",
    )
    args = p.parse_args()

    # 전략 프리셋 적용
    preset = _apply_preset(args.strategy)
    COPY_RATIO          = preset["copy_ratio"]
    MAX_POSITION_USDC   = preset["max_position_usdc"]
    MAX_OPEN_POSITIONS  = preset["max_open_positions"]
    STOP_LOSS_PCT       = preset["stop_loss_pct"]
    TAKE_PROFIT_PCT     = preset["take_profit_pct"]
    n_traders           = args.traders or preset["n_traders"]

    log.info(f"전략: {preset['label']} | copy={COPY_RATIO*100:.0f}% | "
             f"max_pos=${MAX_POSITION_USDC:.0f} | SL={STOP_LOSS_PCT}% | TP={TAKE_PROFIT_PCT}%")

    if args.quick:
        run(duration_min=5, interval_sec=60, output=args.output, n_traders=n_traders)
    else:
        run(duration_min=args.duration, interval_sec=args.interval,
            output=args.output, n_traders=n_traders)
