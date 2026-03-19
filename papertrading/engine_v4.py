"""
Copy Perp Papertrading Engine v4 — 하이브리드 방식 실시간 PnL 추적

설계:
  trades API: symbol 없음 → 포지션 변화 감지 불가
  positions API: symbol 있음 → 열림/닫힘 감지 가능
  → 하이브리드: positions 스냅샷으로 open/close 감지
                trades API에서 최신 가격(마크가격 대용) 추출
                → unrealized PnL 실시간 계산

v3 대비 개선:
  ① 가격 추적: trades API에서 심볼별 최신 체결가 → unrealized PnL 실시간 반영
  ② 포지션 닫힘 시 청산 가격 = 해당 심볼 최신 trades 가격 (entry 아님)
  ③ 4전략 독립 인스턴스로 병렬 실행
  ④ 60초 폴링 (v3의 120초 → 절반)

4전략: conservative / default / balanced / aggressive
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

def get_positions(addr: str) -> list:
    r = _cf_get(f"positions?account={addr}")
    return (r.get("data") or []) if isinstance(r, dict) else (r or [])

def get_trades(addr: str, limit: int = 50) -> list:
    r = _cf_get(f"trades?account={addr}&limit={limit}")
    return (r.get("data") or []) if isinstance(r, dict) else (r or [])

def get_leaderboard(limit: int = 200) -> list:
    r = _cf_get(f"leaderboard?limit={limit}")
    return (r.get("data") or []) if isinstance(r, dict) else (r or [])


# ── 가상 포지션 ───────────────────────────────────────────────────────────────
@dataclass
class VPos:
    symbol:      str
    side:        str       # "long" | "short"
    entry_price: float
    size:        float
    opened_at:   float
    trader:      str
    cur_price:   float = 0.0

    @property
    def notional(self): return self.entry_price * self.size

    def upnl(self, cur: float = None) -> float:
        p = cur or self.cur_price or self.entry_price
        if self.side == "long":  return (p - self.entry_price) * self.size
        else:                    return (self.entry_price - p) * self.size

    def pct(self, cur: float = None) -> float:
        return self.upnl(cur) / self.notional * 100 if self.notional else 0


# ── 전략 인스턴스 ─────────────────────────────────────────────────────────────
@dataclass
class Strategy:
    name:         str
    label:        str
    copy_ratio:   float
    max_pos_usdc: float
    stop_loss:    float
    take_profit:  float
    max_open:     int
    traders:      list        # [address, ...]

    # 런타임 상태
    capital:      float = 10_000.0
    positions:    dict  = field(default_factory=dict)   # key: f"{sym}_{addr8}"
    snapshots:    dict  = field(default_factory=dict)   # addr → prev pos list
    mark_prices:  dict  = field(default_factory=dict)   # symbol → latest price
    closed:       list  = field(default_factory=list)
    wins:         int   = 0
    losses:       int   = 0
    gross_profit: float = 0.0
    gross_loss:   float = 0.0
    max_dd_pct:   float = 0.0
    peak:         float = 10_000.0
    equity_series: list = field(default_factory=list)
    start_time:   float = field(default_factory=time.time)
    log:          object = None

    def __post_init__(self):
        self.log  = logging.getLogger(f"v4[{self.name[:4]}]")
        self.peak = self.capital

    def _key(self, sym: str, addr: str) -> str:
        return f"{sym}_{addr[:8]}"

    # ── positions 스냅샷 비교로 open/close 감지 ─────────────────────────────
    def update_from_positions(self, addr: str, alias: str, new_list: list):
        old_map = {}
        for p in self.snapshots.get(addr, []):
            sym  = p.get("symbol","UNK")
            side = "long" if p.get("side","") in ("bid","long","buy") else "short"
            old_map[(sym, side)] = p

        new_map = {}
        for p in new_list:
            sym  = p.get("symbol","UNK")
            side = "long" if p.get("side","") in ("bid","long","buy") else "short"
            new_map[(sym, side)] = p

        # 신규 포지션 → open
        for (sym, side), p in new_map.items():
            if (sym, side) not in old_map:
                ep  = float(p.get("entry_price") or 0)
                amt = abs(float(p.get("amount") or 0))
                if ep > 0 and amt > 0:
                    self._open(addr, alias, sym, side, ep, amt)

        # 사라진 포지션 → close (최신 mark_price 사용)
        for (sym, side), p in old_map.items():
            if (sym, side) not in new_map:
                key = self._key(sym, addr)
                if key in self.positions:
                    exit_price = self.mark_prices.get(sym) or float(p.get("entry_price") or 0)
                    self._close(key, exit_price, reason="trader_closed")

        # 유지 포지션 → cur_price 업데이트
        for (sym, side), p in new_map.items():
            key = self._key(sym, addr)
            if key in self.positions:
                mp = self.mark_prices.get(sym)
                if mp:
                    self.positions[key].cur_price = mp

        self.snapshots[addr] = new_list

    # ── trades API → mark_prices 업데이트 ────────────────────────────────────
    def update_mark_prices_from_trades(self, addr: str, trades: list):
        """
        trades API에는 symbol 없음.
        포지션 entry_price와 trade price를 비교해 심볼 추론 후 mark_prices 갱신.

        ⚠️ 안전 기준:
        - 오차 3% 이내만 매핑 (기존 15% → 오류 발생원인)
        - 복수 매핑 충돌 시 무시 (1:1 매핑만 허용)
        - 매핑 실패 시 기존 mark_price 유지 (잘못된 가격으로 덮어쓰기 방지)
        """
        if not trades:
            return
        pos_list = self.snapshots.get(addr, [])
        if not pos_list:
            return

        # entry_price → symbol 맵 (중복 entry_price 있으면 제외)
        ep_to_sym: dict[float, str] = {}
        seen_eps: set[float] = set()
        for p in pos_list:
            ep  = float(p.get("entry_price") or 0)
            sym = p.get("symbol", "UNK")
            if ep > 0 and sym != "UNK":
                if ep not in seen_eps:
                    ep_to_sym[ep] = sym
                    seen_eps.add(ep)
                else:
                    # 같은 entry_price를 가진 심볼 2개 → 매핑 불가, 제거
                    ep_to_sym.pop(ep, None)

        for t in trades:
            price = float(t.get("price") or 0)
            if price <= 0:
                continue

            # 오차 3% 이내, 유일한 매핑만 허용
            matches = []
            for ep, sym in ep_to_sym.items():
                diff = abs(ep - price) / ep if ep > 0 else 1.0
                if diff <= 0.03:       # 3% 이내만 신뢰
                    matches.append((diff, sym))

            if len(matches) == 1:
                # 유일한 매핑 → 안전하게 업데이트
                _, sym = matches[0]
                self.mark_prices[sym] = price
            # 매핑 0개 or 2개 이상 → 무시 (신뢰 불가)

    def _open(self, addr, alias, sym, side, ep, trader_amt):
        if len(self.positions) >= self.max_open:
            return
        key = self._key(sym, addr)
        if key in self.positions:
            return

        copy_size = trader_amt * self.copy_ratio
        notional  = copy_size * ep
        if notional > self.max_pos_usdc:
            copy_size = self.max_pos_usdc / ep
            notional  = self.max_pos_usdc
        if notional < 1.0:
            return

        pos = VPos(symbol=sym, side=side, entry_price=ep,
                   size=copy_size, opened_at=time.time(),
                   trader=alias, cur_price=ep)
        self.positions[key] = pos
        self.log.info(f"  📈 OPEN [{alias}] {sym} {side.upper()} "
                      f"{copy_size:.4f} @ ${ep:.4f} = ${notional:.2f}")

    def _close(self, key, exit_price, reason="closed"):
        pos = self.positions.pop(key, None)
        if pos is None:
            return
        if exit_price <= 0:
            exit_price = pos.entry_price

        pnl  = pos.upnl(exit_price)
        pct  = pos.pct(exit_price)
        held = (time.time() - pos.opened_at) / 60
        self.capital += pnl

        if pnl >= 0:
            self.wins += 1;   self.gross_profit += pnl
        else:
            self.losses += 1; self.gross_loss   += pnl

        emoji = "✅" if pnl >= 0 else "❌"
        self.log.info(f"  {emoji} CLOSE [{pos.trader}] {pos.symbol} {pos.side.upper()} "
                      f"진입=${pos.entry_price:.4f}→${exit_price:.4f} "
                      f"PnL=${pnl:+.4f}({pct:+.2f}%) [{held:.1f}분] "
                      f"자본=${self.capital:,.2f} [{reason}]")

        self.closed.append({
            "symbol": pos.symbol, "side": pos.side,
            "entry": pos.entry_price, "exit": exit_price,
            "size": pos.size, "pnl": round(pnl, 4),
            "pct": round(pct, 2), "held_min": round(held, 1),
            "trader": pos.trader, "reason": reason,
        })

    def apply_sl_tp(self):
        """
        SL/TP 강제 청산.

        안전 기준:
        - mark_price 신뢰도 확인: entry_price 대비 10% 이상 괴리 시 무시
          (trades 가격 매핑 오류 방어)
        - 신뢰할 수 있는 가격만 SL/TP 트리거에 사용
        """
        to_close = []
        for key, pos in self.positions.items():
            raw_mark = self.mark_prices.get(pos.symbol)

            # mark_price 신뢰도 검증: entry 대비 10% 이상 벗어나면 무시
            if raw_mark and pos.entry_price > 0:
                drift = abs(raw_mark - pos.entry_price) / pos.entry_price
                if drift > 0.10:    # 10% 초과 괴리 → 매핑 오류 가능성
                    raw_mark = None  # 해당 가격 무시

            cur = raw_mark or pos.cur_price or pos.entry_price
            if cur != pos.entry_price:
                pos.cur_price = cur

            pct = pos.pct(cur)
            if   pct <= -self.stop_loss:   to_close.append((key, cur, "SL"))
            elif pct >= self.take_profit:  to_close.append((key, cur, "TP"))

        for key, cur, reason in to_close:
            self._close(key, cur, reason)

    def update_equity(self):
        unr = sum(p.upnl(self.mark_prices.get(p.symbol) or p.cur_price or p.entry_price)
                  for p in self.positions.values())
        eq  = self.capital + unr
        if eq > self.peak: self.peak = eq
        dd  = (self.peak - eq) / self.peak * 100
        if dd > self.max_dd_pct: self.max_dd_pct = dd
        self.equity_series.append(round(eq, 2))
        return eq

    # ── 속성 ─────────────────────────────────────────────────────────────────
    @property
    def total_pnl(self):    return self.capital - 10_000.0
    @property
    def roi(self):          return self.total_pnl / 10_000.0 * 100
    @property
    def win_rate(self):
        t = self.wins + self.losses
        return self.wins / t if t else 0.0
    @property
    def profit_factor(self):
        if self.gross_loss == 0:
            return float("inf") if self.gross_profit > 0 else 0.0
        return self.gross_profit / abs(self.gross_loss)
    def _safe_price(self, pos: "VPos") -> float:
        """신뢰도 검증된 가격 반환. 10% 이상 괴리 시 entry_price 유지."""
        raw = self.mark_prices.get(pos.symbol) or pos.cur_price
        if raw and pos.entry_price > 0:
            drift = abs(raw - pos.entry_price) / pos.entry_price
            if drift > 0.10:
                return pos.entry_price   # 신뢰 불가 → 보수적으로 entry 유지
        return raw or pos.entry_price

    @property
    def unrealized(self):
        return sum(p.upnl(self._safe_price(p)) for p in self.positions.values())
    @property
    def equity(self): return self.capital + self.unrealized

    def summary(self) -> dict:
        return {
            "strategy":      self.name,
            "label":         self.label,
            "elapsed_min":   round((time.time() - self.start_time) / 60, 1),
            "initial":       10_000.0,
            "capital":       round(self.capital, 4),
            "unrealized":    round(self.unrealized, 4),
            "equity":        round(self.equity, 4),
            "total_pnl":     round(self.total_pnl, 4),
            "roi_pct":       round(self.roi, 4),
            "wins":          self.wins,
            "losses":        self.losses,
            "total_trades":  self.wins + self.losses,
            "win_rate":      round(self.win_rate, 4),
            "profit_factor": round(self.profit_factor, 4)
                             if self.profit_factor != float("inf") else 9999,
            "gross_profit":  round(self.gross_profit, 4),
            "gross_loss":    round(self.gross_loss, 4),
            "max_dd_pct":    round(self.max_dd_pct, 4),
            "open_positions": len(self.positions),
            "equity_series": self.equity_series,
            "closed_trades": self.closed[-50:],  # 최근 50건
            "copy_ratio":    self.copy_ratio,
            "max_pos_usdc":  self.max_pos_usdc,
            "stop_loss":     self.stop_loss,
            "take_profit":   self.take_profit,
        }


# ── 4전략 병렬 실행 ───────────────────────────────────────────────────────────
class MultiStrategyRunner:
    def __init__(self, strategies: list, poll_interval: int = 60):
        self.strategies    = strategies
        self.poll_interval = poll_interval
        self.poll_count    = 0
        self.start_time    = time.time()
        self.log           = logging.getLogger("v4[MASTER]")

        # 트레이더 → 담당 전략 목록
        self._trader_strats: dict[str, list] = {}
        for s in strategies:
            for addr in s.traders:
                self._trader_strats.setdefault(addr, []).append(s)

        # alias 로드
        self._aliases: dict[str, str] = {}
        lb = get_leaderboard(200)
        for t in lb:
            a = t.get("address","")
            self._aliases[a] = t.get("username") or a[:8]

        total_traders = len(self._trader_strats)
        self.log.info(
            f"MultiRunner 초기화 | {len(strategies)}전략 | "
            f"유니크 트레이더 {total_traders}명"
        )
        for s in strategies:
            self.log.info(
                f"  [{s.label}] copy={s.copy_ratio*100:.0f}% "
                f"max_pos=${s.max_pos_usdc} SL={s.stop_loss}% TP={s.take_profit}% "
                f"트레이더={len(s.traders)}명"
            )

    def poll_once(self):
        self.poll_count += 1
        self.log.info(f"\n🔄 폴 #{self.poll_count}")

        for addr, strats in self._trader_strats.items():
            alias = self._aliases.get(addr, addr[:8])
            try:
                # ① positions 조회 → open/close 감지
                pos_list = get_positions(addr)

                # ② trades 조회 → mark_prices 업데이트
                trades   = get_trades(addr, limit=30)

                for s in strats:
                    if trades:
                        s.update_mark_prices_from_trades(addr, trades)
                    s.update_from_positions(addr, alias, pos_list)

                pos_count = len(pos_list)
                self.log.info(f"  [{alias}] 포지션={pos_count} trades={len(trades)}")

            except Exception as e:
                self.log.warning(f"  [{alias}] 오류: {e}")
            time.sleep(0.4)

        # SL/TP + 자산 업데이트
        for s in self.strategies:
            s.apply_sl_tp()
            s.update_equity()

    def print_dashboard(self):
        elapsed = (time.time() - self.start_time) / 60
        print()
        print("=" * 78)
        print(f"  📊 Copy Perp 4전략 병렬 페이퍼트레이딩 v4")
        print(f"     {elapsed:.0f}분 경과 | 폴 {self.poll_count}회 | trades API 가격 추적")
        print("=" * 78)
        print(f"  {'전략':<13} {'ROI':>8} {'실현PnL':>10} {'미실현':>8} "
              f"{'자산':>12} {'W/L':>8} {'WR':>6} {'PF':>6} {'MDD':>6}")
        print(f"  {'-'*13} {'-'*8} {'-'*10} {'-'*8} {'-'*12} {'-'*8} {'-'*6} {'-'*6} {'-'*6}")
        for s in self.strategies:
            pf = f"{s.profit_factor:.2f}" if s.profit_factor != float("inf") else "∞"
            print(
                f"  {s.label:<13} {s.roi:>+7.3f}% "
                f"{s.total_pnl:>+9.2f}$ {s.unrealized:>+7.2f}$ "
                f"{s.equity:>11,.2f}$ "
                f"{s.wins:>3}W/{s.losses:<3}L "
                f"{s.win_rate:>5.0%} {pf:>6} {s.max_dd_pct:>5.2f}%"
            )
        print()
        # 최근 청산 5건 통합
        all_closed = [(s.label, c) for s in self.strategies for c in s.closed]
        all_closed.sort(key=lambda x: x[1].get("held_min", 0))
        if all_closed:
            print("  ── 최근 청산 ──────────────────────────────────────────────────")
            for label, c in all_closed[-5:]:
                emoji = "✅" if c["pnl"] >= 0 else "❌"
                print(f"    {emoji} [{label}] {c['symbol']} {c['side'].upper()} "
                      f"${c['entry']:.4f}→${c['exit']:.4f} "
                      f"PnL=${c['pnl']:+.4f}({c['pct']:+.2f}%) [{c['held_min']}분]")
        print("=" * 78)

    def save_all(self, out_dir: str) -> dict:
        ts = time.strftime("%Y%m%d_%H%M%S")
        os.makedirs(out_dir, exist_ok=True)
        results = {}
        for s in self.strategies:
            fname = f"v4_{s.name}_{ts}.json"
            path  = os.path.join(out_dir, fname)
            data  = s.summary()
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
            results[s.name] = data

        combined = {
            "updated_at":   ts,
            "elapsed_min":  round((time.time() - self.start_time) / 60, 1),
            "poll_count":   self.poll_count,
            "strategies":   results,
        }
        cp = os.path.join(out_dir, "v4_combined_latest.json")
        with open(cp, "w") as f:
            json.dump(combined, f, indent=2)
        self.log.info(f"💾 저장 완료: {cp}")
        return combined


# ── 진입점 ────────────────────────────────────────────────────────────────────
def run_multi(duration_min: int = 480, poll_interval: int = 60, out_dir: str = None):
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from core.strategies import STRATEGY_PRESETS

    if out_dir is None:
        out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "v4_results")

    strategies = []
    for key in ["conservative", "default", "balanced", "aggressive"]:
        p = STRATEGY_PRESETS[key]
        strategies.append(Strategy(
            name         = key,
            label        = p["emoji"] + p["name"],
            copy_ratio   = p["copy_ratio"],
            max_pos_usdc = p["max_position_usdc"],
            stop_loss    = p.get("stop_loss_pct", 8.0),
            take_profit  = p.get("take_profit_pct", 15.0),
            max_open     = p.get("max_open_positions", 10),
            traders      = p["traders"],
        ))

    runner   = MultiStrategyRunner(strategies, poll_interval=poll_interval)
    end_time = time.time() + duration_min * 60
    next_report = time.time() + max(300, poll_interval * 5)

    print(f"\n{'='*60}")
    print(f"  🚀 Copy Perp 4전략 병렬 페이퍼트레이딩 v4 시작")
    print(f"  실행 시간: {duration_min}분 | 폴링: {poll_interval}초")
    print(f"  저장 경로: {out_dir}")
    print(f"{'='*60}\n")

    try:
        while time.time() < end_time:
            runner.poll_once()

            if time.time() >= next_report:
                runner.print_dashboard()
                runner.save_all(out_dir)
                next_report = time.time() + max(300, poll_interval * 5)

            remaining = end_time - time.time()
            if remaining <= 0:
                break
            sleep = min(poll_interval, remaining)
            logging.getLogger("v4[MASTER]").info(
                f"💤 {sleep:.0f}초 대기 (잔여 {remaining/60:.1f}분)")
            time.sleep(sleep)

    except KeyboardInterrupt:
        print("\n🛑 수동 중단")

    runner.print_dashboard()
    return runner.save_all(out_dir)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Copy Perp 4전략 병렬 페이퍼트레이딩 v4")
    ap.add_argument("--duration", type=int, default=480, help="실행 시간(분, 기본 8시간)")
    ap.add_argument("--interval", type=int, default=60,  help="폴링 간격(초, 기본 60)")
    ap.add_argument("--out",      type=str, default=None, help="결과 저장 경로")
    args = ap.parse_args()
    run_multi(duration_min=args.duration, poll_interval=args.interval, out_dir=args.out)
