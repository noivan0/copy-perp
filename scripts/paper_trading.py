#!/usr/bin/env python3
"""
Copy Perp — Mainnet 실시간 Paper Trading 엔진
=============================================
- codetabs 프록시로 Mainnet 실시간 데이터 조회
- 실제 트레이더 포지션 변화 감지 → 가상 복사
- 슬리피지 / 수수료 / 펀딩비 시뮬레이션
- 수익성 & 안정성 지표 실시간 집계

실행: python3 scripts/paper_trading.py [--duration 분] [--capital 금액] [--interval 초]
"""

import os, sys, json, time, ssl, asyncio, argparse, logging, urllib.request, urllib.parse
from dataclasses import dataclass, field, asdict
from typing import Optional
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("paper-trader")

# ── 수수료 (Pacifica 기준) ───────────────────────────
TAKER_FEE    = 0.0006   # 0.06%
MAKER_FEE    = 0.0002   # 0.02%
BUILDER_FEE  = 0.0001   # 0.01% (builder code 수익)
SLIPPAGE_BPS = 5        # 5 bps

# ── 기본값 ──────────────────────────────────────────
DEFAULT_CAPITAL    = 10_000  # USDC
DEFAULT_COPY_RATIO = 0.10
DEFAULT_INTERVAL   = 45      # 초
MAX_POSITION_USD   = 500


# ── API 클라이언트 ───────────────────────────────────

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode    = ssl.CERT_NONE

_last_req = 0.0
_MIN_DELAY = 2.0  # 최소 요청 간격 (초)


def codetabs_get(path: str, extra_delay: float = 0.0) -> list | dict | None:
    """codetabs.com 프록시를 통한 Pacifica Mainnet API GET"""
    global _last_req
    gap = time.time() - _last_req
    if gap < _MIN_DELAY + extra_delay:
        time.sleep(_MIN_DELAY + extra_delay - gap)
    target = f"https://api.pacifica.fi/api/v1/{path}"
    proxy  = f"https://api.codetabs.com/v1/proxy?quest={target}"
    req    = urllib.request.Request(proxy, headers={"User-Agent": "CopyPerp-PaperTrading/1.0"})
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=20) as resp:
            raw = resp.read()
        _last_req = time.time()
        r = json.loads(raw.decode("utf-8", "ignore"))
        return r.get("data") if (isinstance(r, dict) and "data" in r) else r
    except Exception as e:
        _last_req = time.time()
        raise


def get_leaderboard(limit: int = 100) -> list:
    r = codetabs_get(f"leaderboard?limit={limit}")
    return r or []


def get_prices() -> dict[str, float]:
    """심볼 → 마크가격"""
    r = codetabs_get("info/prices") or []
    return {
        p["symbol"]: float(p.get("mark") or p.get("price") or 0)
        for p in r if p.get("symbol")
    }


def get_positions(address: str) -> list:
    try:
        r = codetabs_get(f"positions?account={address}", extra_delay=1.0)
        return r or []
    except Exception as e:
        log.debug(f"포지션 조회 실패 {address[:12]}: {e}")
        return []


# ── 데이터 클래스 ────────────────────────────────────

@dataclass
class Position:
    symbol:      str
    side:        str          # "long" | "short"
    entry_price: float
    size:        float
    usdc_value:  float
    opened_at:   float = field(default_factory=time.time)
    trader_addr: str   = ""

    def upnl(self, cur: float) -> float:
        return (cur - self.entry_price) * self.size if self.side == "long" \
               else (self.entry_price - cur) * self.size

    def upct(self, cur: float) -> float:
        return self.upnl(cur) / self.usdc_value * 100 if self.usdc_value else 0.0


@dataclass
class Trade:
    symbol:      str
    side:        str
    action:      str    # "open" | "close"
    price:       float
    size:        float
    usdc:        float
    fee:         float
    pnl:         float  # close 시 실현 pnl (fee 차감 후)
    ts:          float  = field(default_factory=time.time)
    trader:      str    = ""


@dataclass
class Account:
    initial:     float
    cash:        float
    positions:   dict  = field(default_factory=dict)   # symbol → Position
    trades:      list  = field(default_factory=list)
    total_pnl:   float = 0.0
    total_fees:  float = 0.0
    builder_rev: float = 0.0
    wins:        int   = 0
    losses:      int   = 0
    peak_eq:     float = 0.0

    def equity(self, prices: dict) -> float:
        unreal = sum(
            p.upnl(prices.get(s, p.entry_price))
            for s, p in self.positions.items()
        )
        return self.cash + unreal

    def drawdown(self, prices: dict) -> float:
        eq = self.equity(prices)
        return max(0.0, (self.peak_eq - eq) / self.peak_eq * 100) if self.peak_eq else 0.0

    def win_rate(self) -> float:
        t = self.wins + self.losses
        return self.wins / t * 100 if t else 0.0

    def roi(self) -> float:
        return self.total_pnl / self.initial * 100


# ── Paper Trading 엔진 ───────────────────────────────

class PaperEngine:
    def __init__(self, capital: float, copy_ratio: float, max_pos_usd: float):
        self.acc = Account(initial=capital, cash=capital, peak_eq=capital)
        self.copy_ratio   = copy_ratio
        self.max_pos_usd  = max_pos_usd
        self.prices: dict = {}
        self.traders: list = []
        self.prev_positions: dict = {}   # trader_addr → {symbol: side}

    # ── 트레이더 선정 ──────────────────────────────
    def select_traders(self, leaderboard: list, top_n: int = 5) -> list:
        scored = []
        for t in leaderboard:
            pnl7d  = float(t.get("pnl_7d",  0) or 0)
            pnl30d = float(t.get("pnl_30d", 0) or 0)
            vol7d  = float(t.get("volume_7d", 0) or 0)
            oi     = float(t.get("oi_current", 0) or 0)
            eq     = float(t.get("equity_current", 1) or 1)

            # 필터: 7d 수익 양수 & OI 있음(활성 포지션)
            if pnl7d <= 0:
                continue
            if oi < 5_000:
                continue
            if vol7d < 5_000:
                continue

            # 스코어: 7d ROI 비중 높게
            score = (pnl7d / eq) * 0.6 + (pnl30d / eq) * 0.4
            scored.append({**t, "_score": score})

        scored.sort(key=lambda x: x["_score"], reverse=True)
        sel = scored[:top_n]

        log.info(f"✅ 팔로우 트레이더 {len(sel)}명 선정:")
        for i, t in enumerate(sel, 1):
            addr  = t["address"][:20]
            pnl7d = float(t.get("pnl_7d", 0) or 0)
            oi    = float(t.get("oi_current", 0) or 0)
            log.info(f"  #{i} {addr}... 7d=+${pnl7d:,.0f}  OI=${oi:,.0f}  score={t['_score']:.4f}")

        return sel

    # ── 포지션 오픈 ────────────────────────────────
    def open_pos(self, symbol: str, side: str, trader: str) -> Optional[Trade]:
        if symbol in self.acc.positions:
            existing = self.acc.positions[symbol]
            if existing.side == side:
                return None
            self.close_pos(symbol, "반전")

        price = self.prices.get(symbol, 0)
        if not price:
            log.debug(f"  {symbol} 가격 없음")
            return None

        slip       = price * SLIPPAGE_BPS / 10000
        exec_price = price + slip if side == "long" else price - slip
        usdc       = min(self.acc.cash * self.copy_ratio, self.max_pos_usd)

        if usdc < 5:
            log.debug(f"  USDC 부족 (${usdc:.2f})")
            return None

        size = usdc / exec_price
        fee  = usdc * TAKER_FEE

        self.acc.cash     -= (usdc + fee)
        self.acc.total_fees += fee
        self.acc.builder_rev += usdc * BUILDER_FEE

        pos = Position(symbol=symbol, side=side, entry_price=exec_price,
                       size=size, usdc_value=usdc, trader_addr=trader)
        self.acc.positions[symbol] = pos

        t = Trade(symbol=symbol, side=side, action="open",
                  price=exec_price, size=size, usdc=usdc, fee=fee, pnl=0.0, trader=trader)
        self.acc.trades.append(t)

        arrow = "📈" if side == "long" else "📉"
        log.info(f"  {arrow} OPEN {symbol} {side.upper():5}  ${usdc:,.0f}  @ ${exec_price:,.4f}  fee=${fee:.3f}")
        return t

    # ── 포지션 청산 ────────────────────────────────
    def close_pos(self, symbol: str, reason: str = "") -> Optional[Trade]:
        pos = self.acc.positions.get(symbol)
        if not pos:
            return None

        price      = self.prices.get(symbol, pos.entry_price)
        slip       = price * SLIPPAGE_BPS / 10000
        exec_price = price - slip if pos.side == "long" else price + slip

        gross_pnl  = pos.upnl(exec_price)
        fee        = pos.usdc_value * TAKER_FEE
        net_pnl    = gross_pnl - fee

        self.acc.cash        += pos.usdc_value + gross_pnl - fee
        self.acc.total_pnl   += net_pnl
        self.acc.total_fees  += fee
        self.acc.builder_rev += pos.usdc_value * BUILDER_FEE

        if net_pnl > 0:
            self.acc.wins   += 1
        else:
            self.acc.losses += 1

        t = Trade(symbol=symbol, side=pos.side, action="close",
                  price=exec_price, size=pos.size, usdc=pos.usdc_value,
                  fee=fee, pnl=net_pnl, trader=pos.trader_addr)
        self.acc.trades.append(t)

        emoji = "✅" if net_pnl > 0 else "❌"
        log.info(f"  {emoji} CLOSE {symbol} {pos.side.upper():5}  PnL=${net_pnl:+.2f}  ({reason})")
        del self.acc.positions[symbol]
        return t

    def close_all(self, reason: str = "종료"):
        for sym in list(self.acc.positions.keys()):
            self.close_pos(sym, reason)

    # ── 트레이더 동기화 ────────────────────────────
    def sync(self, trader: dict) -> int:
        addr     = trader["address"]
        pos_list = get_positions(addr)

        current: dict[str, str] = {}
        for p in pos_list:
            sym  = p.get("symbol", "")
            raw  = p.get("side", "")
            side = "long" if raw == "bid" else "short" if raw == "ask" else ""
            if sym and side:
                current[sym] = side

        prev    = self.prev_positions.get(addr, {})
        changes = 0

        for sym, side in current.items():
            if sym not in prev:
                log.info(f"  🔔 신규: {addr[:14]}... {sym} {side}")
                self.open_pos(sym, side, addr)
                changes += 1
            elif prev[sym] != side:
                log.info(f"  🔄 반전: {addr[:14]}... {sym} {prev[sym]}→{side}")
                self.open_pos(sym, side, addr)
                changes += 1

        for sym in list(prev.keys()):
            if sym not in current and sym in self.acc.positions:
                log.info(f"  🔔 청산 감지: {addr[:14]}... {sym}")
                self.close_pos(sym, "트레이더 청산")
                changes += 1

        self.prev_positions[addr] = current
        return changes

    def update_peak(self):
        eq = self.acc.equity(self.prices)
        if eq > self.acc.peak_eq:
            self.acc.peak_eq = eq

    # ── 스냅샷 ─────────────────────────────────────
    def snapshot(self) -> dict:
        eq  = self.acc.equity(self.prices)
        dd  = self.acc.drawdown(self.prices)
        roi = self.acc.roi()
        wr  = self.acc.win_rate()

        open_pos = [
            {
                "symbol": sym,
                "side":   pos.side,
                "entry":  pos.entry_price,
                "cur":    self.prices.get(sym, pos.entry_price),
                "upnl":   pos.upnl(self.prices.get(sym, pos.entry_price)),
                "upct":   pos.upct(self.prices.get(sym, pos.entry_price)),
                "usdc":   pos.usdc_value,
                "age_m":  (time.time() - pos.opened_at) / 60,
                "trader": pos.trader_addr[:14] + "...",
            }
            for sym, pos in self.acc.positions.items()
        ]

        return {
            "ts":          datetime.now(timezone.utc).isoformat(),
            "equity":      eq,
            "cash":        self.acc.cash,
            "initial":     self.acc.initial,
            "pnl":         self.acc.total_pnl,
            "roi":         roi,
            "drawdown":    dd,
            "peak_eq":     self.acc.peak_eq,
            "win_rate":    wr,
            "wins":        self.acc.wins,
            "losses":      self.acc.losses,
            "total_trades": self.acc.wins + self.acc.losses,
            "fees":        self.acc.total_fees,
            "builder_rev": self.acc.builder_rev,
            "open_cnt":    len(self.acc.positions),
            "open_pos":    open_pos,
            "n_traders":   len(self.traders),
        }


# ── 메인 루프 ─────────────────────────────────────

async def run(
    duration_min: int   = 60,
    capital:      float = DEFAULT_CAPITAL,
    interval_sec: int   = DEFAULT_INTERVAL,
    copy_ratio:   float = DEFAULT_COPY_RATIO,
    max_pos_usd:  float = MAX_POSITION_USD,
):
    engine = PaperEngine(capital, copy_ratio, max_pos_usd)
    base   = os.path.dirname(os.path.dirname(__file__))

    log.info("=" * 60)
    log.info("Copy Perp — Mainnet Paper Trading")
    log.info(f"  자본: ${capital:,.0f}  복사율: {copy_ratio*100:.0f}%  최대포지션: ${max_pos_usd:,.0f}")
    log.info(f"  기간: {duration_min}분  |  폴링: {interval_sec}초")
    log.info("=" * 60)

    # ── 리더보드 조회 ──────────────────────────────
    log.info("\n📊 Mainnet 리더보드 조회...")
    try:
        lb = get_leaderboard(100)
        log.info(f"  {len(lb)}명 조회")
    except Exception as e:
        log.warning(f"  리더보드 실패({e}), 저장 데이터 사용")
        saved = json.load(open(os.path.join(base, "mainnet_trader_analysis.json")))
        lb = saved["traders"]

    engine.traders = engine.select_traders(lb, top_n=5)
    if not engine.traders:
        log.error("팔로우 트레이더 없음")
        return

    # ── 가격 초기화 ────────────────────────────────
    log.info("\n💹 실시간 가격 조회...")
    try:
        engine.prices = get_prices()
        btc = engine.prices.get("BTC", 0)
        eth = engine.prices.get("ETH", 0)
        sol = engine.prices.get("SOL", 0)
        log.info(f"  {len(engine.prices)}개 심볼  BTC=${btc:,.0f}  ETH=${eth:,.0f}  SOL=${sol:,.2f}")
    except Exception as e:
        log.warning(f"  가격 조회 실패({e}), 이전 데이터로 대체")
        engine.prices = {"BTC": 70000, "ETH": 2100, "SOL": 88}

    # ── 초기 포지션 스냅샷 ─────────────────────────
    log.info("\n🔄 초기 포지션 동기화...")
    for t in engine.traders:
        pos_list = get_positions(t["address"])
        current  = {}
        for p in pos_list:
            sym  = p.get("symbol", "")
            raw  = p.get("side", "")
            side = "long" if raw == "bid" else "short" if raw == "ask" else ""
            if sym and side:
                current[sym] = side
        engine.prev_positions[t["address"]] = current
        log.info(f"  {t['address'][:18]}... 포지션 {len(current)}개")
        for sym, side in list(current.items())[:3]:
            engine.open_pos(sym, side, t["address"])

    # ── 메인 루프 ──────────────────────────────────
    end      = time.time() + duration_min * 60
    cycle    = 0
    history  = []
    report_every = 5 * 60  # 5분
    last_rep = time.time()

    log.info(f"\n🚀 Paper Trading 시작! ({duration_min}분)\n")

    while time.time() < end:
        cycle += 1
        now_str = datetime.now().strftime("%H:%M:%S")
        log.info(f"\n{'─'*55}")
        log.info(f"사이클 #{cycle:3d}  |  {now_str}  |  남은 {(end-time.time())/60:.1f}분")

        # 가격 업데이트
        try:
            engine.prices = get_prices()
            log.info(f"  💹 BTC=${engine.prices.get('BTC',0):,.0f}  ETH=${engine.prices.get('ETH',0):,.0f}  SOL=${engine.prices.get('SOL',0):,.2f}")
        except Exception as e:
            log.warning(f"  가격 업데이트 실패: {e}")

        # 트레이더 동기화
        changes = 0
        for t in engine.traders:
            try:
                changes += engine.sync(t)
            except Exception as e:
                log.debug(f"  동기화 실패 {t['address'][:12]}: {e}")

        engine.update_peak()
        snap = engine.snapshot()
        history.append(snap)

        # 현황 출력
        eq  = snap["equity"]
        roi = snap["roi"]
        dd  = snap["drawdown"]
        pnl = snap["pnl"]
        wr  = snap["win_rate"]
        log.info(f"  💰 자산=${eq:,.2f}  ROI={roi:+.2f}%  DD={dd:.2f}%  PnL=${pnl:+.2f}")
        log.info(f"  📊 포지션={snap['open_cnt']}  거래={snap['total_trades']}건  승률={wr:.1f}%  빌더={snap['builder_rev']:.4f}")

        if snap["open_pos"]:
            log.info("  오픈 포지션:")
            for p in snap["open_pos"]:
                bar = "↑" if p["side"] == "long" else "↓"
                col = "+" if p["upnl"] >= 0 else ""
                log.info(f"    {bar} {p['symbol']:6}  ${p['usdc']:>6,.0f}  uPnL={col}${p['upnl']:.2f}({p['upct']:+.2f}%)  {p['age_m']:.0f}분")

        # 변화 없으면 표시
        if changes == 0:
            log.info("  → 포지션 변화 없음")

        # 중간 보고 (5분마다)
        if time.time() - last_rep >= report_every:
            _print_report(snap, is_mid=True)
            last_rep = time.time()

        # 대기
        wait = min(interval_sec, end - time.time())
        if wait > 0:
            await asyncio.sleep(wait)

    # ── 최종 정산 ──────────────────────────────────
    engine.close_all("세션 종료")
    final = engine.snapshot()
    history.append(final)

    _print_report(final, is_mid=False)

    # 결과 저장
    result = {
        "session": {
            "ts":            datetime.now(timezone.utc).isoformat(),
            "duration_min":  duration_min,
            "capital":       capital,
            "copy_ratio":    copy_ratio,
            "max_pos_usd":   max_pos_usd,
            "cycles":        cycle,
            "traders":       [t["address"] for t in engine.traders],
        },
        "final": final,
        "trades": [asdict(t) for t in engine.acc.trades],
        "history": history,
    }
    out = os.path.join(base, "papertrading_result.json")
    json.dump(result, open(out, "w"), indent=2, default=str)
    log.info(f"\n💾 결과 저장: {out}")

    return result


def _print_report(snap: dict, is_mid: bool = True):
    tag  = "📊 중간 보고" if is_mid else "🏁 최종 결과"
    eq   = snap["equity"]
    roi  = snap["roi"]
    dd   = snap["drawdown"]
    pnl  = snap["pnl"]
    wr   = snap["win_rate"]
    fees = snap["fees"]
    rev  = snap["builder_rev"]

    risk = "🟢 낮음" if dd < 5 else ("🟡 중간" if dd < 15 else "🔴 높음")
    log.info(f"\n{'='*55}")
    log.info(f"  {tag}  {snap['ts']}")
    log.info(f"  자산:   ${snap['initial']:>10,.2f} → ${eq:>10,.2f}")
    log.info(f"  PnL:    ${pnl:>+10,.2f}    ROI: {roi:>+8.2f}%")
    log.info(f"  낙폭:   {dd:>10.2f}%    리스크: {risk}")
    log.info(f"  거래:   {snap['total_trades']:>10}건    승률: {wr:>7.1f}%")
    log.info(f"  수수료: ${fees:>10.4f}    빌더:  ${rev:>10.6f}")
    log.info(f"  포지션: {snap['open_cnt']}개  |  팔로우: {snap['n_traders']}명")
    log.info("="*55)


# ── Telegram 보고 메시지 ────────────────────────────

def telegram_report(snap: dict, is_final: bool = False) -> str:
    tag  = "🏁 *최종 결과*" if is_final else "📊 *중간 보고*"
    eq   = snap["equity"]
    pnl  = snap["pnl"]
    roi  = snap["roi"]
    dd   = snap["drawdown"]
    wr   = snap["win_rate"]
    fees = snap["fees"]
    rev  = snap["builder_rev"]
    cap  = snap["initial"]

    ei = "🟢" if roi >= 0 else "🔴"
    risk = "낮음🟢" if dd < 5 else ("중간🟡" if dd < 15 else "높음🔴")

    lines = [
        f"Copy Perp Paper Trading {tag}",
        "",
        f"💰 *자산 현황*",
        f"  초기: ${cap:,.0f} → 현재: ${eq:,.2f}",
        f"  실현PnL: ${pnl:+,.2f}  {ei} ROI: {roi:+.2f}%",
        f"  최대낙폭: {dd:.2f}% (리스크: {risk})",
        "",
        f"📊 *거래 통계*",
        f"  총 {snap['total_trades']}건  승률: {wr:.1f}%  (승{snap['wins']}/패{snap['losses']})",
        f"  수수료: ${fees:.2f}  |  빌더수익: ${rev:.4f}",
        "",
        f"🏦 *오픈 포지션* ({snap['open_cnt']}개)",
    ]

    for p in snap["open_pos"][:6]:
        bar = "↑롱" if p["side"] == "long" else "↓숏"
        col = "🟢" if p["upnl"] >= 0 else "🔴"
        lines.append(f"  {col} {p['symbol']:5} {bar}  ${p['usdc']:,.0f}  uPnL: ${p['upnl']:+.2f} ({p['upct']:+.2f}%)")

    lines.extend([
        "",
        f"👥 팔로우: {snap['n_traders']}명",
        f"⏰ {datetime.now().strftime('%H:%M:%S')} KST",
    ])
    return "\n".join(lines)


# ── CLI ────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration",  type=int,   default=60)
    parser.add_argument("--capital",   type=float, default=DEFAULT_CAPITAL)
    parser.add_argument("--interval",  type=int,   default=DEFAULT_INTERVAL)
    parser.add_argument("--ratio",     type=float, default=DEFAULT_COPY_RATIO)
    parser.add_argument("--max-pos",   type=float, default=MAX_POSITION_USD)
    args = parser.parse_args()

    return await run(
        duration_min=args.duration,
        capital=args.capital,
        interval_sec=args.interval,
        copy_ratio=args.ratio,
        max_pos_usd=args.max_pos,
    )


if __name__ == "__main__":
    asyncio.run(main())
