"""
Copy Perp Papertrading Engine v4 — trades API 기반 실시간 PnL 추적

핵심 변경 (v3 → v4):
  - positions 스냅샷 방식 폐기 → trades API 폴링으로 교체
  - created_at 커서 기반 신규 체결만 처리 (중복 없음)
  - open/close 이벤트 완전 포착 (2분 안에 빠른 거래도 누락 없음)
  - 실시간 unrealized PnL: entry_price vs 최신 trades price 비교
  - 4전략 동시 실행 지원 (Strategy 인스턴스 독립)
"""
import json, os, ssl, socket, time, logging
from dataclasses import dataclass, field
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
    datefmt="%H:%M:%S",
)

# ── API ──────────────────────────────────────────────────────────────────────
CF_HOST  = "do5jt23sqak4.cloudfront.net"
PAC_HOST = "api.pacifica.fi"
PORT     = 443

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode    = ssl.CERT_NONE

def _cf_get(path: str, timeout: int = 12, retries: int = 3):
    for attempt in range(retries):
        try:
            sock  = socket.create_connection((CF_HOST, PORT), timeout=timeout)
            ssock = _ssl_ctx.wrap_socket(sock, server_hostname=CF_HOST)
            req   = (f"GET /api/v1/{path} HTTP/1.1\r\nHost: {PAC_HOST}\r\n"
                     f"Accept: application/json\r\nConnection: close\r\n\r\n")
            ssock.sendall(req.encode())
            data  = b""
            ssock.settimeout(timeout)
            while True:
                chunk = ssock.recv(32768)
                if not chunk: break
                data += chunk
            ssock.close(); sock.close()
            if b"\r\n\r\n" not in data:
                continue
            body = data.split(b"\r\n\r\n", 1)[1]
            return json.loads(body.decode("utf-8", "ignore"))
        except Exception as e:
            time.sleep(1.0 * (attempt + 1))
    return None

def get_trades(address: str, limit: int = 100):
    r = _cf_get(f"trades?account={address}&limit={limit}")
    if isinstance(r, dict):
        return r.get("data") or []
    return r or []

def get_positions(address: str):
    r = _cf_get(f"positions?account={address}")
    if isinstance(r, dict):
        return r.get("data") or []
    return r or []

def get_leaderboard(limit: int = 100):
    r = _cf_get(f"leaderboard?limit={limit}")
    if isinstance(r, dict):
        return r.get("data") or []
    return r or []


# ── 가상 포지션 ───────────────────────────────────────────────────────────────
@dataclass
class VPos:
    symbol:      str
    side:        str       # "long" | "short"
    entry_price: float
    size:        float     # copy 수량
    opened_at:   float
    trader:      str
    last_price:  float = 0.0

    @property
    def notional(self): return self.entry_price * self.size

    def upnl(self, cur: float = None) -> float:
        p = cur or self.last_price or self.entry_price
        return (p - self.entry_price) * self.size if self.side == "long" \
               else (self.entry_price - p) * self.size

    def pct(self, cur: float = None) -> float:
        return self.upnl(cur) / self.notional * 100 if self.notional else 0


# ── 전략 인스턴스 ─────────────────────────────────────────────────────────────
@dataclass
class StrategyState:
    name:         str
    label:        str
    copy_ratio:   float
    max_pos_usdc: float
    stop_loss:    float
    take_profit:  float
    max_open:     int
    traders:      list    # 트레이더 주소 리스트
    capital:      float   = 10_000.0

    # 내부 상태
    positions:    dict  = field(default_factory=dict)   # key: f"{symbol}_{addr8}"
    closed:       list  = field(default_factory=list)
    cursors:      dict  = field(default_factory=dict)   # addr → last created_at
    latest_price: dict  = field(default_factory=dict)   # symbol → last price

    wins:         int   = 0
    losses:       int   = 0
    gross_profit: float = 0.0
    gross_loss:   float = 0.0
    max_dd_pct:   float = 0.0
    peak:         float = 10_000.0
    equity_series: list = field(default_factory=list)
    start_time:   float = field(default_factory=time.time)

    log: logging.Logger = field(default=None)

    def __post_init__(self):
        self.log = logging.getLogger(f"PT4[{self.name}]")
        self.peak = self.capital

    # ── 거래 이벤트 처리 ─────────────────────────────────────────────────────
    def _side_of(self, event_side: str) -> Optional[str]:
        m = {"open_long": "long", "open_short": "short",
             "close_long": "long", "close_short": "short",
             "bid": "long", "ask": "short",
             "long": "long", "short": "short"}
        s = m.get(event_side)
        if s is None:
            if "long"  in event_side: return "long"
            if "short" in event_side: return "short"
        return s

    def _pos_key(self, symbol: str, addr: str) -> str:
        return f"{symbol}_{addr[:8]}"

    def on_trade(self, addr: str, alias: str, trade: dict):
        event = trade.get("event_type", "")
        side_raw = trade.get("side", "")
        price  = float(trade.get("price") or 0)
        amount = float(trade.get("amount") or 0)
        symbol = trade.get("symbol") or "UNK"

        if price <= 0 or amount <= 0:
            return

        # 최신 가격 업데이트 (unrealized 계산용)
        self.latest_price[symbol] = price

        is_open  = "open"  in side_raw
        is_close = "close" in side_raw
        side     = self._side_of(side_raw)

        if is_open and side:
            self._open(addr, alias, symbol, side, price, amount)
        elif is_close and side:
            self._close(addr, symbol, side, price)

    def _open(self, addr, alias, symbol, side, price, trader_amount):
        if len(self.positions) >= self.max_open:
            return
        key = self._pos_key(symbol, addr)
        if key in self.positions:
            return  # 이미 추적 중

        # copy 수량 계산
        copy_size = trader_amount * self.copy_ratio
        notional  = copy_size * price
        if notional > self.max_pos_usdc:
            copy_size = self.max_pos_usdc / price
            notional  = self.max_pos_usdc
        if notional < 1.0:
            return

        pos = VPos(symbol=symbol, side=side, entry_price=price,
                   size=copy_size, opened_at=time.time(),
                   trader=alias, last_price=price)
        self.positions[key] = pos
        self.log.info(f"  📈 OPEN  [{alias}] {symbol} {side.upper()} "
                      f"size={copy_size:.4f} @ ${price:.4f} = ${notional:.2f}")

    def _close(self, addr, symbol, side, exit_price):
        key = self._pos_key(symbol, addr)
        pos = self.positions.pop(key, None)
        if pos is None:
            return

        pnl = pos.upnl(exit_price)
        pct = pos.pct(exit_price)
        self.capital += pnl
        held = (time.time() - pos.opened_at) / 60

        if pnl >= 0:
            self.wins += 1
            self.gross_profit += pnl
        else:
            self.losses += 1
            self.gross_loss += pnl

        emoji = "✅" if pnl >= 0 else "❌"
        self.log.info(f"  {emoji} CLOSE [{pos.trader}] {symbol} {pos.side.upper()} "
                      f"진입=${pos.entry_price:.4f} → 청산=${exit_price:.4f} "
                      f"PnL=${pnl:+.4f} ({pct:+.2f}%) [{held:.1f}분] "
                      f"자본=${self.capital:,.2f}")

        self.closed.append({
            "symbol": symbol, "side": pos.side,
            "entry": pos.entry_price, "exit": exit_price,
            "size": pos.size, "pnl": round(pnl, 4), "pct": round(pct, 2),
            "held_min": round(held, 1), "trader": pos.trader,
        })

    def apply_sl_tp(self):
        """현재 마크가격 기준 SL/TP 강제 청산"""
        to_close = []
        for key, pos in self.positions.items():
            cur = self.latest_price.get(pos.symbol) or pos.entry_price
            pos.last_price = cur
            pct = pos.pct(cur)
            if pct <= -self.stop_loss:
                to_close.append((key, cur, "SL"))
            elif pct >= self.take_profit:
                to_close.append((key, cur, "TP"))
        # positions dict에서 pop하므로 루프 밖에서 처리
        for key, cur, reason in to_close:
            pos = self.positions.pop(key, None)
            if not pos: continue
            pnl = pos.upnl(cur)
            self.capital += pnl
            if pnl >= 0: self.wins += 1; self.gross_profit += pnl
            else: self.losses += 1; self.gross_loss += pnl
            self.log.info(f"  🔔 {reason} [{pos.trader}] {pos.symbol} "
                          f"PnL=${pnl:+.4f} ({pos.pct(cur):+.2f}%) 자본=${self.capital:,.2f}")

    def update_equity(self):
        unrealized = sum(
            p.upnl(self.latest_price.get(p.symbol) or p.entry_price)
            for p in self.positions.values()
        )
        equity = self.capital + unrealized
        if equity > self.peak: self.peak = equity
        dd = (self.peak - equity) / self.peak * 100
        if dd > self.max_dd_pct: self.max_dd_pct = dd
        self.equity_series.append(round(equity, 2))
        return equity

    # ── 리포트 ───────────────────────────────────────────────────────────────
    @property
    def total_pnl(self): return self.capital - 10_000.0
    @property
    def roi(self): return self.total_pnl / 10_000.0 * 100
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
        return sum(p.upnl(self.latest_price.get(p.symbol) or p.entry_price)
                   for p in self.positions.values())
    @property
    def equity(self): return self.capital + self.unrealized

    def summary(self) -> dict:
        elapsed = (time.time() - self.start_time) / 60
        return {
            "strategy":       self.name,
            "label":          self.label,
            "elapsed_min":    round(elapsed, 1),
            "initial":        10_000.0,
            "capital":        round(self.capital, 4),
            "unrealized":     round(self.unrealized, 4),
            "equity":         round(self.equity, 4),
            "total_pnl":      round(self.total_pnl, 4),
            "roi_pct":        round(self.roi, 4),
            "wins":           self.wins,
            "losses":         self.losses,
            "total_trades":   self.wins + self.losses,
            "win_rate":       round(self.win_rate, 4),
            "profit_factor":  round(self.profit_factor, 4) if self.profit_factor != float("inf") else 9999,
            "gross_profit":   round(self.gross_profit, 4),
            "gross_loss":     round(self.gross_loss, 4),
            "max_dd_pct":     round(self.max_dd_pct, 4),
            "open_positions": len(self.positions),
            "equity_series":  self.equity_series,
            "closed_trades":  self.closed,
            "copy_ratio":     self.copy_ratio,
            "max_pos_usdc":   self.max_pos_usdc,
            "stop_loss":      self.stop_loss,
            "take_profit":    self.take_profit,
        }

    def print_status(self):
        elapsed = (time.time() - self.start_time) / 60
        t = self.wins + self.losses
        pf = f"{self.profit_factor:.2f}x" if self.profit_factor != float("inf") else "∞"
        print(f"  [{self.label}] "
              f"ROI={self.roi:+.3f}% PnL=${self.total_pnl:+.2f} "
              f"자산=${self.equity:,.2f} "
              f"W/L={self.wins}/{self.losses}({self.win_rate:.0%}) PF={pf} "
              f"포지션={len(self.positions)} DD={self.max_dd_pct:.2f}% "
              f"[{elapsed:.0f}분]")


# ── 4전략 병렬 실행 엔진 ─────────────────────────────────────────────────────
class MultiStrategyEngine:
    def __init__(self, strategies: list[StrategyState], poll_interval: int = 60):
        self.strategies    = strategies
        self.poll_interval = poll_interval
        self.poll_count    = 0
        self.start_time    = time.time()
        self.log           = logging.getLogger("PT4[MASTER]")

        # 트레이더 주소 → 해당 전략 목록 매핑
        self._trader_map: dict[str, list[StrategyState]] = {}
        for s in strategies:
            for addr in s.traders:
                self._trader_map.setdefault(addr, []).append(s)

        # 트레이더별 alias (leaderboard에서 조회)
        self._aliases: dict[str, str] = {}
        self._load_aliases()

        # 커서 초기화: 현재 시각 기준 (이후 체결만 처리)
        now_ms = int(time.time() * 1000)
        for addr in self._trader_map:
            for s in strategies:
                s.cursors[addr] = now_ms

        self.log.info(f"MultiStrategy 초기화 | {len(strategies)}전략 | 트레이더 {len(self._trader_map)}명")
        for s in strategies:
            self.log.info(f"  [{s.label}] copy={s.copy_ratio*100:.0f}% max_pos=${s.max_pos_usdc} "
                          f"SL={s.stop_loss}% TP={s.take_profit}% 트레이더={len(s.traders)}명")

    def _load_aliases(self):
        lb = get_leaderboard(200)
        for t in lb:
            addr = t.get("address", "")
            alias = t.get("username") or addr[:8]
            self._aliases[addr] = alias
        self.log.info(f"리더보드 별칭 {len(self._aliases)}개 로드")

    def poll_once(self):
        self.poll_count += 1
        self.log.info(f"\n🔄 폴 #{self.poll_count}")

        # 트레이더별 신규 trades 조회
        for addr, strats in self._trader_map.items():
            alias = self._aliases.get(addr, addr[:8])
            try:
                trades = get_trades(addr, limit=100)
                if not trades:
                    continue

                new_count = 0
                for s in strats:
                    cursor = s.cursors.get(addr, 0)
                    new_trades = [t for t in trades if t.get("created_at", 0) > cursor]
                    # created_at 오름차순 (오래된 것 먼저)
                    new_trades.sort(key=lambda x: x.get("created_at", 0))

                    for t in new_trades:
                        # symbol 없으면 price로 역추론 불가 — 스킵
                        sym = t.get("symbol")
                        if not sym:
                            # trades API에 symbol이 없는 경우, 포지션 API로 매핑
                            t["symbol"] = self._guess_symbol(addr, t)
                        s.on_trade(addr, alias, t)

                    if new_trades:
                        s.cursors[addr] = max(t.get("created_at", 0) for t in new_trades)
                        new_count = len(new_trades)

                if new_count:
                    self.log.info(f"  [{alias}] 신규 {new_count}건 처리")

            except Exception as e:
                self.log.warning(f"  [{alias}] 오류: {e}")
            time.sleep(0.3)

        # SL/TP 적용
        for s in self.strategies:
            s.apply_sl_tp()

        # 자산 업데이트
        for s in self.strategies:
            s.update_equity()

    def _guess_symbol(self, addr: str, trade: dict) -> str:
        """trades API에 symbol 없는 경우: 현재 포지션 entry_price와 price 비교로 추론"""
        price = float(trade.get("price") or 0)
        if price <= 0:
            return "UNK"
        try:
            positions = get_positions(addr)
            for p in positions:
                ep = float(p.get("entry_price") or 0)
                if ep > 0 and abs(ep - price) / ep < 0.05:  # 5% 오차 내
                    return p.get("symbol", "UNK")
        except:
            pass
        return "UNK"

    def print_dashboard(self):
        elapsed = (time.time() - self.start_time) / 60
        print()
        print("=" * 72)
        print(f"  📊 Copy Perp 4전략 병렬 페이퍼트레이딩 | {elapsed:.0f}분 경과 | 폴 {self.poll_count}회")
        print("=" * 72)
        print(f"  {'전략':<12} {'ROI':>8} {'PnL':>10} {'자산':>12} {'W/L':>8} {'WR':>6} {'PF':>6} {'DD':>6}")
        print(f"  {'-'*12} {'-'*8} {'-'*10} {'-'*12} {'-'*8} {'-'*6} {'-'*6} {'-'*6}")
        for s in self.strategies:
            t = s.wins + s.losses
            pf = f"{s.profit_factor:.2f}" if s.profit_factor != float("inf") else "∞"
            print(f"  {s.label:<12} {s.roi:>+7.3f}% {s.total_pnl:>+9.2f}$ "
                  f"{s.equity:>11,.2f}$ {s.wins:>3}W/{s.losses:<3}L "
                  f"{s.win_rate:>5.0%} {pf:>6} {s.max_dd_pct:>5.2f}%")
        print("=" * 72)
        # 최근 청산 통합 (최신 5건)
        all_closed = []
        for s in self.strategies:
            for c in s.closed:
                all_closed.append((s.label, c))
        all_closed.sort(key=lambda x: x[1].get("held_min", 0))
        if all_closed:
            print(f"  최근 청산:")
            for label, c in all_closed[-5:]:
                emoji = "✅" if c["pnl"] >= 0 else "❌"
                print(f"    {emoji} [{label}] {c['symbol']} {c['side'].upper()} "
                      f"진입=${c['entry']:.4f}→${c['exit']:.4f} "
                      f"PnL=${c['pnl']:+.4f} ({c['pct']:+.2f}%) [{c['held_min']}분]")
        print()

    def save_all(self, out_dir: str):
        ts = time.strftime("%Y%m%d_%H%M%S")
        results = {}
        for s in self.strategies:
            path = os.path.join(out_dir, f"v4_{s.name}_{ts}.json")
            data = s.summary()
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
            results[s.name] = data

        # 통합 요약
        summary_path = os.path.join(out_dir, "v4_combined_latest.json")
        with open(summary_path, "w") as f:
            json.dump({
                "timestamp": ts,
                "elapsed_min": round((time.time() - self.start_time) / 60, 1),
                "strategies": results,
            }, f, indent=2)
        self.log.info(f"💾 저장: {summary_path}")
        return results


def run_multi(duration_min: int = 120, poll_interval: int = 60, out_dir: str = None):
    """4전략 병렬 페이퍼트레이딩 실행"""
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from core.strategies import STRATEGY_PRESETS

    if out_dir is None:
        out_dir = os.path.dirname(os.path.abspath(__file__))

    # 4전략 인스턴스 생성
    strategies = []
    for key in ["conservative", "default", "balanced", "aggressive"]:
        p = STRATEGY_PRESETS[key]
        s = StrategyState(
            name         = key,
            label        = p["emoji"] + p["name"],
            copy_ratio   = p["copy_ratio"],
            max_pos_usdc = p["max_position_usdc"],
            stop_loss    = p.get("stop_loss_pct", 8.0),
            take_profit  = p.get("take_profit_pct", 15.0),
            max_open     = p.get("max_open_positions", 10),
            traders      = p["traders"],
        )
        strategies.append(s)

    engine = MultiStrategyEngine(strategies, poll_interval=poll_interval)

    end_time    = time.time() + duration_min * 60
    next_report = time.time() + max(300, poll_interval * 5)

    print(f"\n🚀 4전략 병렬 페이퍼트레이딩 시작 | {duration_min}분 실행")
    print(f"   폴링 간격: {poll_interval}초 | trades API 실시간 추적\n")

    try:
        while time.time() < end_time:
            engine.poll_once()

            if time.time() >= next_report:
                engine.print_dashboard()
                engine.save_all(out_dir)
                next_report = time.time() + max(300, poll_interval * 5)

            remaining = end_time - time.time()
            if remaining <= 0:
                break
            sleep = min(poll_interval, remaining)
            logging.getLogger("PT4[MASTER]").info(
                f"💤 {sleep:.0f}초 대기 (잔여 {remaining/60:.1f}분)")
            time.sleep(sleep)

    except KeyboardInterrupt:
        print("\n🛑 중단")

    engine.print_dashboard()
    return engine.save_all(out_dir)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Copy Perp 4전략 병렬 페이퍼트레이딩 v4")
    p.add_argument("--duration",  type=int, default=120, help="실행 시간(분)")
    p.add_argument("--interval",  type=int, default=60,  help="폴링 간격(초)")
    p.add_argument("--out",       type=str, default=None, help="결과 저장 디렉토리")
    args = p.parse_args()
    run_multi(duration_min=args.duration, poll_interval=args.interval, out_dir=args.out)
