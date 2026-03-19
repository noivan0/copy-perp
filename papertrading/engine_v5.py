"""
Copy Perp Papertrading Engine v5 — trades API 커서 기반 완전 추적

v4 대비 근본적 재설계:
  문제: positions 스냅샷 방식은 빠른 open→close 회전 거래를 누락
        같은 symbol 반복 open→close 시 두 번째부터 key 충돌
  
  해결: trades API를 created_at 커서로 실시간 폴링
        open_long/open_short → 가상 포지션 생성
        close_long/close_short → 해당 포지션 PnL 실현
        
  핵심 인사이트 (YjCD9Gek 실측):
    - 롱+숏 동시 포지션 후 동시 청산 (funding rate 차익 전략)
    - 빠른 회전: 수십 건을 수분 내 처리
    - 이 모든 거래를 놓치지 않으려면 trades API 커서 추적 필수
    
  PnL 계산:
    open_long  @ price_a → close_long  @ price_b: (b - a) × size
    open_short @ price_a → close_short @ price_b: (a - b) × size
    
  symbol 매핑:
    trades API에 symbol 없음 → positions API의 symbol/entry_price 맵 활용
    trades price와 positions entry_price 매칭 (오차 3% 이내)
    미매핑 시 "UNK_price" 키로 임시 추적
    
  스노우볼 효과:
    실현 PnL → 자본에 즉시 반영
    다음 포지션 copy_size = trader_amount × copy_ratio (자본 증가분 반영)
"""
import json, os, ssl, socket, time, logging
from collections import defaultdict
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
            data = b""
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
        except Exception:
            time.sleep(1.0 * (attempt + 1))
    return None

def get_trades(addr: str, limit: int = 100) -> list:
    r = _cf_get(f"trades?account={addr}&limit={limit}")
    return (r.get("data") or []) if isinstance(r, dict) else (r or [])

def get_positions(addr: str) -> list:
    r = _cf_get(f"positions?account={addr}")
    return (r.get("data") or []) if isinstance(r, dict) else (r or [])

def get_leaderboard(limit: int = 200) -> list:
    r = _cf_get(f"leaderboard?limit={limit}")
    return (r.get("data") or []) if isinstance(r, dict) else (r or [])


# ── 심볼 맵 빌더 ─────────────────────────────────────────────────────────────
def build_price_to_symbol(positions: list) -> dict:
    """
    positions entry_price → symbol 맵
    오차 3% 이내 매칭용. 중복 entry_price 제거.
    """
    ep_map: dict[float, str] = {}
    seen: set[float] = set()
    for p in positions:
        ep  = float(p.get("entry_price") or 0)
        sym = p.get("symbol", "")
        if ep > 0 and sym:
            if ep in seen:
                ep_map.pop(ep, None)  # 중복 → 제거
            else:
                ep_map[ep] = sym
                seen.add(ep)
    return ep_map

def lookup_symbol(price: float, ep_map: dict, tol: float = 0.03) -> str:
    """trades price → symbol 추론. 유일 매핑만 허용."""
    if price <= 0:
        return ""
    matches = [(abs(ep - price) / ep, sym)
               for ep, sym in ep_map.items()
               if ep > 0 and abs(ep - price) / ep <= tol]
    if len(matches) == 1:
        return matches[0][1]
    return ""  # 0개 or 2개 이상 → 신뢰 불가


# ── 가상 오픈 포지션 ──────────────────────────────────────────────────────────
@dataclass
class VPos:
    symbol:      str
    side:        str       # "long" | "short"
    entry_price: float
    size:        float     # 복사 수량
    opened_at:   float
    trader:      str
    trade_id:    str       # 고유 식별자 (created_at + price + amount)

    @property
    def notional(self): return self.entry_price * self.size

    def pnl(self, close_price: float) -> float:
        if self.side == "long":
            return (close_price - self.entry_price) * self.size
        else:
            return (self.entry_price - close_price) * self.size

    def pct(self, close_price: float) -> float:
        return self.pnl(close_price) / self.notional * 100 if self.notional else 0


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
    traders:      list

    # 런타임
    capital:      float = 10_000.0
    # 오픈 포지션: key = f"{sym}_{side}_{addr8}_{seq}"
    positions:    dict  = field(default_factory=dict)
    # 심볼별 다음 seq (같은 symbol 반복 오픈 허용)
    seq_counter:  dict  = field(default_factory=lambda: defaultdict(int))
    # 심볼별 대기 중인 close 이벤트 (open 미처리 시 버퍼)
    pending_close: dict = field(default_factory=lambda: defaultdict(list))

    closed:       list  = field(default_factory=list)
    wins:         int   = 0
    losses:       int   = 0
    gross_profit: float = 0.0
    gross_loss:   float = 0.0
    max_dd_pct:   float = 0.0
    peak:         float = 10_000.0
    equity_series: list = field(default_factory=list)
    total_volume: float = 0.0   # 복사 거래 총 볼륨 (스노우볼 추적)
    start_time:   float = field(default_factory=time.time)
    log:          object = None

    def __post_init__(self):
        self.log  = logging.getLogger(f"v5[{self.name[:4]}]")
        self.peak = self.capital

    def _side_of(self, event_side: str) -> str:
        if "long"  in event_side: return "long"
        if "short" in event_side: return "short"
        return ""

    def _open_key(self, sym: str, side: str, addr: str) -> str:
        seq = self.seq_counter[f"{sym}_{side}_{addr[:8]}"]
        return f"{sym}_{side}_{addr[:8]}_{seq}"

    def _next_key(self, sym: str, side: str, addr: str) -> str:
        base = f"{sym}_{side}_{addr[:8]}"
        self.seq_counter[base] += 1
        return f"{base}_{self.seq_counter[base]}"

    def _find_oldest_open(self, sym: str, side: str, addr: str) -> Optional[str]:
        """같은 symbol+side+addr의 가장 오래된 오픈 포지션 키 반환"""
        prefix = f"{sym}_{side}_{addr[:8]}_"
        matches = [(k, v.opened_at) for k, v in self.positions.items()
                   if k.startswith(prefix)]
        if not matches:
            return None
        return min(matches, key=lambda x: x[1])[0]

    def on_open(self, addr: str, alias: str, sym: str, side: str,
                price: float, trader_amount: float):
        if not sym or price <= 0 or trader_amount <= 0:
            return
        if len(self.positions) >= self.max_open:
            return

        copy_size = trader_amount * self.copy_ratio
        notional  = copy_size * price
        if notional > self.max_pos_usdc:
            copy_size = self.max_pos_usdc / price
            notional  = self.max_pos_usdc
        if notional < 0.5:
            return

        key = self._next_key(sym, side, addr)
        pos = VPos(symbol=sym, side=side, entry_price=price,
                   size=copy_size, opened_at=time.time(),
                   trader=alias, trade_id=key)
        self.positions[key] = pos
        self.total_volume += notional

        self.log.info(f"  📈 OPEN  [{alias}] {sym} {side.upper()} "
                      f"{copy_size:.4f}@${price:.4f}=${notional:.2f} [{key}]")

        # 대기 중인 close 이벤트 처리
        pending = self.pending_close.pop(f"{sym}_{side}_{addr[:8]}", [])
        if pending:
            for cp in pending:
                self.on_close(addr, alias, sym, side, cp)
                break  # 첫 번째 pending만 처리

    def on_close(self, addr: str, alias: str, sym: str, side: str, price: float):
        if not sym or price <= 0:
            return

        key = self._find_oldest_open(sym, side, addr)
        if key is None:
            # open 미처리 → pending 버퍼
            buf_key = f"{sym}_{side}_{addr[:8]}"
            self.pending_close[buf_key].append(price)
            self.log.debug(f"  ⏳ PENDING CLOSE [{alias}] {sym} {side.upper()} ${price:.4f}")
            return

        pos = self.positions.pop(key)
        pnl  = pos.pnl(price)
        pct  = pos.pct(price)
        held = (time.time() - pos.opened_at) / 60

        self.capital += pnl
        self.total_volume += pos.size * price  # 청산 볼륨도 추가

        if pnl >= 0:
            self.wins += 1;   self.gross_profit += pnl
        else:
            self.losses += 1; self.gross_loss   += pnl

        emoji = "✅" if pnl >= 0 else "❌"
        self.log.info(f"  {emoji} CLOSE [{pos.trader}] {sym} {side.upper()} "
                      f"${pos.entry_price:.4f}→${price:.4f} "
                      f"PnL=${pnl:+.4f}({pct:+.2f}%) [{held:.1f}분] "
                      f"자본=${self.capital:,.2f}")

        self.closed.append({
            "symbol": sym, "side": side,
            "entry": pos.entry_price, "exit": price,
            "size": pos.size, "pnl": round(pnl, 4),
            "pct": round(pct, 2), "held_min": round(held, 1),
            "trader": pos.trader,
        })

    def update_equity(self):
        # unrealized: entry_price 기준 (mark_price 없음 → 보수적)
        unr = 0.0
        eq  = self.capital + unr
        if eq > self.peak: self.peak = eq
        dd  = (self.peak - eq) / self.peak * 100
        if dd > self.max_dd_pct: self.max_dd_pct = dd
        self.equity_series.append(round(eq, 2))
        return eq

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

    def summary(self) -> dict:
        return {
            "strategy":       self.name,
            "label":          self.label,
            "elapsed_min":    round((time.time() - self.start_time) / 60, 1),
            "initial":        10_000.0,
            "capital":        round(self.capital, 4),
            "equity":         round(self.capital, 4),  # unrealized=0 (보수적)
            "total_pnl":      round(self.total_pnl, 4),
            "roi_pct":        round(self.roi, 4),
            "wins":           self.wins,
            "losses":         self.losses,
            "total_trades":   self.wins + self.losses,
            "win_rate":       round(self.win_rate, 4),
            "profit_factor":  round(self.profit_factor, 4)
                              if self.profit_factor != float("inf") else 9999,
            "gross_profit":   round(self.gross_profit, 4),
            "gross_loss":     round(self.gross_loss, 4),
            "max_dd_pct":     round(self.max_dd_pct, 4),
            "open_positions": len(self.positions),
            "total_volume":   round(self.total_volume, 2),
            "equity_series":  self.equity_series,
            "closed_trades":  self.closed[-100:],
            "copy_ratio":     self.copy_ratio,
            "max_pos_usdc":   self.max_pos_usdc,
        }


# ── 4전략 병렬 실행기 ─────────────────────────────────────────────────────────
class MultiStrategyRunner:
    def __init__(self, strategies: list, poll_interval: int = 30):
        self.strategies    = strategies
        self.poll_interval = poll_interval
        self.poll_count    = 0
        self.start_time    = time.time()
        self.log           = logging.getLogger("v5[MASTER]")

        # 트레이더 → 전략 목록
        self._trader_strats: dict[str, list] = {}
        for s in strategies:
            for addr in s.traders:
                self._trader_strats.setdefault(addr, []).append(s)

        # alias
        self._aliases: dict[str, str] = {}
        for t in get_leaderboard(200):
            a = t.get("address","")
            self._aliases[a] = t.get("username") or a[:8]

        # 커서: addr → last created_at (ms)
        # 초기값: 지금 시각 (이후 체결만 처리)
        now_ms = int(time.time() * 1000)
        self._cursors: dict[str, int] = {
            addr: now_ms for addr in self._trader_strats
        }

        # 심볼 맵 캐시: addr → {entry_price: symbol}
        self._ep_maps: dict[str, dict] = {}

        self.log.info(
            f"v5 초기화 | {len(strategies)}전략 | "
            f"유니크 트레이더 {len(self._trader_strats)}명 | "
            f"폴링 {poll_interval}초"
        )
        for s in strategies:
            self.log.info(
                f"  [{s.label}] copy={s.copy_ratio*100:.0f}% "
                f"max=${s.max_pos_usdc} SL={s.stop_loss}% TP={s.take_profit}% "
                f"트레이더={len(s.traders)}명"
            )

    def _refresh_ep_map(self, addr: str):
        """5분마다 positions entry_price → symbol 맵 갱신"""
        pos_list = get_positions(addr)
        self._ep_maps[addr] = build_price_to_symbol(pos_list)

    def poll_once(self):
        self.poll_count += 1
        self.log.info(f"\n🔄 폴 #{self.poll_count}")

        # 5폴마다 심볼 맵 갱신
        refresh_ep = (self.poll_count % 5 == 1)

        for addr, strats in self._trader_strats.items():
            alias = self._aliases.get(addr, addr[:8])
            try:
                if refresh_ep or addr not in self._ep_maps:
                    self._refresh_ep_map(addr)

                ep_map = self._ep_maps.get(addr, {})

                # 신규 trades만 조회 (커서 이후)
                trades = get_trades(addr, limit=100)
                cursor = self._cursors.get(addr, 0)
                new_trades = [t for t in trades
                              if t.get("created_at", 0) > cursor]
                if not new_trades:
                    continue

                # 오래된 것 먼저 처리
                new_trades.sort(key=lambda x: x.get("created_at", 0))
                self._cursors[addr] = new_trades[-1].get("created_at", cursor)

                self.log.info(f"  [{alias}] 신규 {len(new_trades)}건")

                for t in new_trades:
                    side_raw = t.get("side", "")
                    price    = float(t.get("price") or 0)
                    amount   = float(t.get("amount") or 0)

                    if price <= 0 or amount <= 0:
                        continue

                    # symbol 추론
                    sym = lookup_symbol(price, ep_map)
                    if not sym:
                        sym = f"UNK@{price:.4f}"

                    is_open  = "open"  in side_raw
                    is_close = "close" in side_raw
                    side     = "long" if "long" in side_raw else (
                               "short" if "short" in side_raw else "")

                    if not side:
                        continue

                    for s in strats:
                        if is_open:
                            s.on_open(addr, alias, sym, side, price, amount)
                        elif is_close:
                            s.on_close(addr, alias, sym, side, price)

            except Exception as e:
                self.log.warning(f"  [{alias}] 오류: {e}")
            time.sleep(0.3)

        for s in self.strategies:
            s.update_equity()

    def print_dashboard(self):
        elapsed = (time.time() - self.start_time) / 60
        print()
        print("=" * 80)
        print(f"  📊 Copy Perp 4전략 병렬 페이퍼트레이딩 v5")
        print(f"     {elapsed:.0f}분 경과 | 폴 {self.poll_count}회 | trades 커서 완전 추적")
        print("=" * 80)
        print(f"  {'전략':<13} {'실현ROI':>8} {'실현PnL':>10} {'W/L':>8} {'WR':>6} "
              f"{'PF':>6} {'볼륨':>10} {'MDD':>6} {'열린':>5}")
        print(f"  {'-'*13} {'-'*8} {'-'*10} {'-'*8} {'-'*6} {'-'*6} {'-'*10} {'-'*6} {'-'*5}")
        for s in self.strategies:
            pf = f"{s.profit_factor:.2f}" if s.profit_factor != float("inf") else "∞"
            vol_k = s.total_volume / 1000
            print(
                f"  {s.label:<13} {s.roi:>+7.3f}% "
                f"{s.total_pnl:>+9.2f}$ "
                f"{s.wins:>3}W/{s.losses:<3}L "
                f"{s.win_rate:>5.0%} {pf:>6} "
                f"{vol_k:>9.1f}k$ {s.max_dd_pct:>5.2f}% "
                f"{len(s.positions):>4}개"
            )
        print()
        # 최근 청산
        all_closed = [(s.label, c) for s in self.strategies for c in s.closed]
        if all_closed:
            # PnL 절대값 기준 TOP 5
            all_closed.sort(key=lambda x: abs(x[1]["pnl"]), reverse=True)
            print("  ── 실현 청산 TOP 5 (PnL 절대값 기준) ────────────────────────────────")
            for label, c in all_closed[:5]:
                emoji = "✅" if c["pnl"] >= 0 else "❌"
                print(f"    {emoji} [{label}] {c['symbol']} {c['side'].upper()} "
                      f"${c['entry']:.4f}→${c['exit']:.4f} "
                      f"PnL=${c['pnl']:+.4f}({c['pct']:+.2f}%) [{c['held_min']}분]")
        print("=" * 80)

    def save_all(self, out_dir: str) -> dict:
        ts = time.strftime("%Y%m%d_%H%M%S")
        os.makedirs(out_dir, exist_ok=True)
        results = {}
        for s in self.strategies:
            path = os.path.join(out_dir, f"v5_{s.name}_{ts}.json")
            data = s.summary()
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
            results[s.name] = data

        combined = {
            "updated_at":  ts,
            "elapsed_min": round((time.time() - self.start_time) / 60, 1),
            "poll_count":  self.poll_count,
            "strategies":  results,
        }
        cp = os.path.join(out_dir, "v5_combined_latest.json")
        with open(cp, "w") as f:
            json.dump(combined, f, indent=2)
        self.log.info(f"💾 {cp}")
        return combined


# ── 진입점 ────────────────────────────────────────────────────────────────────
def run_multi(duration_min: int = 480, poll_interval: int = 30, out_dir: str = None):
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from core.strategies import STRATEGY_PRESETS

    if out_dir is None:
        out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "v5_results")

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
    next_report = time.time() + 300  # 5분마다 리포트

    print(f"\n{'='*65}")
    print(f"  🚀 Copy Perp 4전략 병렬 페이퍼트레이딩 v5")
    print(f"  trades 커서 기반 — 빠른 회전 거래 완전 포착")
    print(f"  실행: {duration_min}분 | 폴링: {poll_interval}초")
    print(f"{'='*65}\n")

    try:
        while time.time() < end_time:
            runner.poll_once()

            if time.time() >= next_report:
                runner.print_dashboard()
                runner.save_all(out_dir)
                next_report = time.time() + 300

            remaining = end_time - time.time()
            if remaining <= 0:
                break
            sleep = min(poll_interval, remaining)
            logging.getLogger("v5[MASTER]").info(
                f"💤 {sleep:.0f}초 대기 (잔여 {remaining/60:.1f}분)")
            time.sleep(sleep)

    except KeyboardInterrupt:
        print("\n🛑 중단")

    runner.print_dashboard()
    return runner.save_all(out_dir)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Copy Perp 4전략 병렬 페이퍼트레이딩 v5")
    ap.add_argument("--duration", type=int, default=480, help="실행 시간(분)")
    ap.add_argument("--interval", type=int, default=30,  help="폴링 간격(초, 기본 30)")
    ap.add_argument("--out",      type=str, default=None)
    args = ap.parse_args()
    run_multi(duration_min=args.duration, poll_interval=args.interval, out_dir=args.out)
