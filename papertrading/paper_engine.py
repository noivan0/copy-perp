"""
Copy Perp — Mainnet Papertrading Engine v2
trades/history API (symbol + entry_price + pnl 포함) 기반
실제 mainnet 체결 데이터로 가상 포트폴리오 시뮬레이션

API: GET /trades/history?account=<addr>&limit=100
필드: history_id, order_id, symbol, amount, price, entry_price, fee, pnl,
      event_type, side, created_at, cause
"""
import json
import os
import ssl
import socket
import time
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("papertrading")


# ── Mainnet API (CloudFront SNI 우회) ─────────────────────
CF_HOST = "do5jt23sqak4.cloudfront.net"
PAC_HOST = "api.pacifica.fi"
PORT = 443

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def _cf_get(path: str, max_retries: int = 3, timeout: int = 15) -> dict | list | None:
    for attempt in range(max_retries):
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
                chunk = ssock.recv(16384)
                if not chunk:
                    break
                data += chunk
            ssock.close()
            sock.close()

            if b"\r\n\r\n" in data:
                body = data.split(b"\r\n\r\n", 1)[1]
            else:
                body = data
            # chunked 처리
            if body and body[0:1].isdigit() and b"\r\n" in body[:16]:
                try:
                    size_line, rest = body.split(b"\r\n", 1)
                    chunk_size = int(size_line.strip(), 16)
                    body = rest[:chunk_size]
                except Exception:
                    pass
            return json.loads(body.decode("utf-8", "ignore"))
        except Exception as e:
            log.debug(f"API 오류 (시도 {attempt+1}/{max_retries}): {e}")
            time.sleep(1.0 * (attempt + 1))
    return None


def get_trade_history(address: str, limit: int = 100) -> list:
    """trades/history API — symbol, entry_price, pnl 포함"""
    r = _cf_get(f"trades/history?account={address}&limit={limit}")
    if isinstance(r, dict):
        return r.get("data", []) or []
    if isinstance(r, list):
        return r
    return []


def get_leaderboard(limit: int = 100) -> list:
    r = _cf_get(f"leaderboard?limit={limit}")
    if isinstance(r, dict):
        return r.get("data", []) or []
    return []


# ── 실제 Mainnet 상위 트레이더 (리더보드 실시간 기준) ─────
# 초기값: 2026-03-14 기준 실제 mainnet 리더보드 TOP5 + 추가 우수 트레이더
DEFAULT_TRADERS = [
    {"address": "4TYEjn9PSpxoBNBXufeuNDRbytzvyyZtEUgXYSk8kYLZ", "alias": "Alpha4TY",  "weight": 0.30, "roi": 1028.7},
    {"address": "YjCD9Gek6MVY9t3MLEGYYdZLeaF6MZrpgZraayWsv9E",  "alias": "YjCD_168",  "weight": 0.25, "roi": 168.4},
    {"address": "HtC4WT6JhKz8eojNigfiqSWykG74kfQmyDeM9f753aPQ", "alias": "HtC4_109",  "weight": 0.20, "roi": 109.2},
    {"address": "6ZjWoJKeD88JqREHhYAWSZVLQfVcMSbx6eVdajXt9Xbv", "alias": "6ZjW_171",  "weight": 0.15, "roi": 171.7},
    {"address": "E8j5xSbGXEWtj7BQobPtiMAfh7CpqR8t1tXX7qtAWCiZ", "alias": "E8j5_160",  "weight": 0.10, "roi": 160.2},
]

# ── 가상 포트폴리오 설정 ──────────────────────────────────
INITIAL_CAPITAL = 10_000.0   # 가상 초기 자본 $10,000
COPY_RATIO = 0.05            # 트레이더 pnl의 5% 복사 (자본 비율 기준)
MAX_POSITION_USDC = 300.0    # 단일 포지션 최대 $300
MIN_ORDER_USDC = 1.0         # 최소 주문 금액 $1


@dataclass
class ClosedTrade:
    history_id: int
    trader: str
    symbol: str
    side: str
    amount: float
    price: float
    entry_price: float
    raw_pnl: float       # 트레이더 실제 PnL (USD)
    copy_pnl: float      # 팔로워 가상 PnL
    copy_ratio_applied: float
    created_at: int
    cause: str


@dataclass
class Portfolio:
    capital: float = INITIAL_CAPITAL
    peak_capital: float = INITIAL_CAPITAL
    closed_trades: list = field(default_factory=list)
    wins: int = 0
    losses: int = 0
    break_even: int = 0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    max_drawdown_pct: float = 0.0
    total_orders: int = 0
    skipped: int = 0

    @property
    def total_pnl(self) -> float:
        return self.capital - INITIAL_CAPITAL

    @property
    def roi_pct(self) -> float:
        return self.total_pnl / INITIAL_CAPITAL * 100

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return self.wins / total if total > 0 else 0.0

    @property
    def profit_factor(self) -> float:
        if self.gross_loss == 0:
            return 999.0 if self.gross_profit > 0 else 0.0
        return self.gross_profit / abs(self.gross_loss)

    @property
    def avg_win(self) -> float:
        return self.gross_profit / self.wins if self.wins > 0 else 0.0

    @property
    def avg_loss(self) -> float:
        return self.gross_loss / self.losses if self.losses > 0 else 0.0


class PaperTradingEngine:
    def __init__(self, traders: list = None):
        self.traders = traders or DEFAULT_TRADERS
        self.portfolio = Portfolio()
        self.seen_ids: set = set()
        self.session_start = time.time()
        self.poll_count = 0
        # 트레이더별 통계
        self.trader_stats: dict = {
            t["alias"]: {"pnl": 0.0, "trades": 0, "wins": 0} for t in self.traders
        }
        log.info(f"📋 Papertrading v2 시작")
        log.info(f"   초기 자본: ${INITIAL_CAPITAL:,.0f} | copy_ratio={COPY_RATIO}")
        log.info(f"   추적 트레이더: {len(self.traders)}명")
        for t in self.traders:
            log.info(f"     [{t['alias']}] ROI={t['roi']:.0f}% weight={t['weight']}")

    def _calc_copy_ratio(self, trader_equity: float, trader_pnl: float, weight: float) -> float:
        """
        팔로워의 복사 비율 계산
        기본: COPY_RATIO * weight
        트레이더 equity가 크면 상대적으로 스케일 다운
        """
        base = COPY_RATIO * weight
        # $10k 팔로워 자본 vs 트레이더 equity 비율로 스케일
        if trader_equity > 0:
            scale = min(1.0, INITIAL_CAPITAL / trader_equity)
            return base * scale
        return base

    def process_history_trade(self, trader: dict, trade: dict, trader_equity: float = 0) -> bool:
        """
        trades/history 단건 처리
        반환: True=처리됨, False=스킵
        """
        hist_id = trade.get("history_id")
        if hist_id and hist_id in self.seen_ids:
            return False
        if hist_id:
            self.seen_ids.add(hist_id)

        symbol = trade.get("symbol", "UNKNOWN")
        side = trade.get("side", "")
        cause = trade.get("cause", "normal")

        # 청산 스킵
        if cause == "liquidation":
            return False

        # open 체결은 PnL 발생 없음 → 추적만
        if "open" in side:
            return False

        # close/fulfill 체결만 PnL 계산
        pnl_raw = trade.get("pnl")
        if pnl_raw is None:
            return False

        try:
            raw_pnl = float(pnl_raw)
        except (TypeError, ValueError):
            return False

        amount = float(trade.get("amount", 0) or 0)
        price = float(trade.get("price", 0) or 0)
        entry_price = float(trade.get("entry_price", 0) or 0)

        if amount <= 0:
            return False

        # 주문 금액 계산 (포지션 크기 USD)
        position_usdc = amount * entry_price if entry_price > 0 else amount * price

        # 최소 금액 필터
        if position_usdc * COPY_RATIO * trader["weight"] < MIN_ORDER_USDC and abs(raw_pnl) < 0.01:
            self.portfolio.skipped += 1
            return False

        # 복사 비율 계산
        ratio = self._calc_copy_ratio(trader_equity, raw_pnl, trader["weight"])

        # 팔로워 PnL = 트레이더 PnL * 비율
        copy_pnl = raw_pnl * ratio

        # max_position 클램핑
        if position_usdc * ratio > MAX_POSITION_USDC and position_usdc > 0:
            # 포지션 클램핑 시 PnL도 비례 조정
            clamped_ratio = MAX_POSITION_USDC / position_usdc
            copy_pnl = raw_pnl * clamped_ratio
            ratio = clamped_ratio

        # 포트폴리오 업데이트
        self.portfolio.capital += copy_pnl
        self.portfolio.total_orders += 1

        if copy_pnl > 0.001:
            self.portfolio.wins += 1
            self.portfolio.gross_profit += copy_pnl
        elif copy_pnl < -0.001:
            self.portfolio.losses += 1
            self.portfolio.gross_loss += copy_pnl
        else:
            self.portfolio.break_even += 1

        # drawdown
        if self.portfolio.capital > self.portfolio.peak_capital:
            self.portfolio.peak_capital = self.portfolio.capital
        dd = (self.portfolio.peak_capital - self.portfolio.capital) / self.portfolio.peak_capital * 100
        if dd > self.portfolio.max_drawdown_pct:
            self.portfolio.max_drawdown_pct = dd

        # 트레이더별 통계
        alias = trader["alias"]
        self.trader_stats[alias]["pnl"] += copy_pnl
        self.trader_stats[alias]["trades"] += 1
        if copy_pnl > 0:
            self.trader_stats[alias]["wins"] += 1

        # 기록
        ct = ClosedTrade(
            history_id=hist_id or 0,
            trader=alias,
            symbol=symbol,
            side=side,
            amount=amount,
            price=price,
            entry_price=entry_price,
            raw_pnl=raw_pnl,
            copy_pnl=round(copy_pnl, 4),
            copy_ratio_applied=round(ratio, 6),
            created_at=trade.get("created_at", 0),
            cause=cause,
        )
        self.portfolio.closed_trades.append(asdict(ct))

        emoji = "✅" if copy_pnl > 0 else "❌" if copy_pnl < 0 else "➖"
        log.info(
            f"  {emoji} [{alias}] {symbol} {side} "
            f"amt={amount:.3f} @ ${price:,.4f} "
            f"trader_pnl={raw_pnl:+.2f} copy_pnl={copy_pnl:+.4f} "
            f"자본=${self.portfolio.capital:,.2f}"
        )
        return True

    def poll_once(self, max_per_trader: int = 100):
        """1회 폴링 — 모든 트레이더 체결 내역 수집·처리"""
        self.poll_count += 1
        p = self.portfolio
        log.info(
            f"\n🔄 폴링 #{self.poll_count} | "
            f"자본=${p.capital:,.2f} | ROI={p.roi_pct:+.2f}% | "
            f"처리={p.total_orders}건"
        )

        new_trades = 0
        for trader in self.traders:
            try:
                history = get_trade_history(trader["address"], limit=max_per_trader)
                log.info(f"  [{trader['alias']}] 내역 {len(history)}건 조회")
                for t in history:
                    if self.process_history_trade(trader, t):
                        new_trades += 1
            except Exception as e:
                log.warning(f"  [{trader['alias']}] 조회 실패: {e}")
            time.sleep(0.4)

        log.info(f"  → 신규 처리: {new_trades}건")
        return new_trades

    def print_report(self, verbose: bool = False):
        p = self.portfolio
        elapsed = time.time() - self.session_start
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        total_trades = p.wins + p.losses + p.break_even

        print()
        print("=" * 65)
        print(f"  📊 COPY PERP MAINNET PAPERTRADING 리포트")
        print(f"  세션: {mins}분 {secs}초 | 폴링: {self.poll_count}회 | {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 65)
        print(f"💰 자본 현황")
        print(f"   초기 자본    : ${INITIAL_CAPITAL:>12,.2f}")
        print(f"   현재 자본    : ${p.capital:>12,.2f}")
        print(f"   실현 PnL     : ${p.total_pnl:>+12.2f}  ({p.roi_pct:+.2f}%)")
        print(f"   최대 드로우다운: {p.max_drawdown_pct:.2f}%")
        print()
        print(f"📈 거래 성과")
        print(f"   완료 거래    : {total_trades}건 (승:{p.wins} | 패:{p.losses} | 본전:{p.break_even})")
        print(f"   승률         : {p.win_rate:.1%}")
        print(f"   수익 인수    : {p.profit_factor:.2f}x")
        print(f"   평균 이익    : ${p.avg_win:>+.4f}")
        print(f"   평균 손실    : ${p.avg_loss:>+.4f}")
        print(f"   총 이익      : ${p.gross_profit:>+.4f}")
        print(f"   총 손실      : ${p.gross_loss:>+.4f}")
        print()
        print(f"⚙️  실행 통계")
        print(f"   주문 처리    : {p.total_orders}건")
        print(f"   스킵         : {p.skipped}건 (소액 필터)")
        print()
        print(f"👥 트레이더별 기여")
        for t in self.traders:
            alias = t["alias"]
            s = self.trader_stats[alias]
            if s["trades"] == 0:
                continue
            wr = s["wins"] / s["trades"] if s["trades"] > 0 else 0
            print(f"   [{alias}] 거래={s['trades']}건 PnL={s['pnl']:+.4f} 승률={wr:.0%}")

        if verbose and p.closed_trades:
            print()
            print(f"📜 최근 체결 거래 (최대 10건)")
            for ct in p.closed_trades[-10:]:
                print(
                    f"   [{ct['trader']}] {ct['symbol']:6} {ct['side']:12} "
                    f"trader={ct['raw_pnl']:+.2f} copy={ct['copy_pnl']:+.4f}"
                )
        print("=" * 65)

        # 안정성 평가
        print()
        print("🔍 안정성 평가")
        issues = []
        if p.max_drawdown_pct > 20:
            issues.append(f"⚠️  최대 드로우다운 {p.max_drawdown_pct:.1f}% > 20% (위험)")
        if p.win_rate < 0.4 and total_trades > 10:
            issues.append(f"⚠️  승률 {p.win_rate:.1%} < 40% (낮음)")
        if p.profit_factor < 1.0 and total_trades > 10:
            issues.append(f"⚠️  수익 인수 {p.profit_factor:.2f} < 1.0 (손실 구간)")
        if not issues:
            print("   ✅ 이상 없음 — 드로우다운 양호, 수익 안정")
        else:
            for issue in issues:
                print(f"   {issue}")
        print()

    def save_result(self, path: str) -> dict:
        p = self.portfolio
        result = {
            "generated_at": time.time(),
            "session_duration_min": (time.time() - self.session_start) / 60,
            "poll_count": self.poll_count,
            "config": {
                "initial_capital": INITIAL_CAPITAL,
                "copy_ratio": COPY_RATIO,
                "max_position_usdc": MAX_POSITION_USDC,
                "traders": self.traders,
            },
            "portfolio": {
                "final_capital": round(p.capital, 4),
                "total_pnl": round(p.total_pnl, 4),
                "roi_pct": round(p.roi_pct, 4),
                "win_rate": round(p.win_rate, 4),
                "wins": p.wins,
                "losses": p.losses,
                "break_even": p.break_even,
                "profit_factor": round(p.profit_factor, 4),
                "avg_win": round(p.avg_win, 4),
                "avg_loss": round(p.avg_loss, 4),
                "gross_profit": round(p.gross_profit, 4),
                "gross_loss": round(p.gross_loss, 4),
                "max_drawdown_pct": round(p.max_drawdown_pct, 4),
                "total_orders": p.total_orders,
                "skipped": p.skipped,
            },
            "trader_stats": self.trader_stats,
            "closed_trades": p.closed_trades,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False, default=str)
        log.info(f"💾 결과 저장: {path}")
        return result


def run_papertrading(
    duration_minutes: int = 60,
    poll_interval_seconds: int = 120,
    output_path: str = None,
    use_leaderboard: bool = True,
):
    """
    Mainnet papertrading 실행
    duration_minutes: 총 실행 시간 (0 = 1회 실행 후 종료)
    poll_interval_seconds: 폴링 간격 (초)
    use_leaderboard: True면 실시간 리더보드에서 트레이더 갱신
    """
    traders = DEFAULT_TRADERS

    # 실시간 리더보드에서 최신 트레이더 조회
    if use_leaderboard:
        log.info("📡 Mainnet 리더보드 조회 중...")
        lb = get_leaderboard(100)
        if lb:
            valid = [t for t in lb if float(t.get("pnl_all_time", 0) or 0) > 0]
            valid.sort(key=lambda x: float(x.get("pnl_all_time", 0) or 0), reverse=True)
            top = valid[:5]
            if len(top) >= 3:
                # 가중치 배분 (ROI 기반)
                equities = [float(t.get("equity_current", 1) or 1) for t in top]
                pnls = [float(t.get("pnl_all_time", 0) or 0) for t in top]
                rois = [p / e * 100 for p, e in zip(pnls, equities)]
                total_roi = sum(max(r, 0) for r in rois) or 1
                weights = [max(r, 0) / total_roi for r in rois]
                # 정규화
                total_w = sum(weights) or 1
                weights = [w / total_w for w in weights]
                traders = [
                    {
                        "address": t["address"],
                        "alias": t["address"][:8],
                        "weight": round(w, 2),
                        "roi": round(roi, 1),
                        "equity": float(t.get("equity_current", 0)),
                    }
                    for t, w, roi in zip(top, weights, rois)
                ]
                log.info(f"✅ 리더보드에서 {len(traders)}명 선정")

    engine = PaperTradingEngine(traders=traders)

    if output_path is None:
        ts = time.strftime("%Y%m%d_%H%M%S")
        base = os.path.dirname(os.path.abspath(__file__))
        output_path = os.path.join(base, f"result_{ts}.json")

    if duration_minutes == 0:
        # 1회 실행 모드
        engine.poll_once(max_per_trader=100)
        engine.print_report(verbose=True)
        return engine.save_result(output_path)

    # 반복 실행 모드
    end_time = time.time() + duration_minutes * 60
    report_every = max(5, duration_minutes // 4)  # 중간 리포트 주기(분)
    last_report = time.time()

    log.info(f"⏱  실행: {duration_minutes}분 | 간격: {poll_interval_seconds}초")

    try:
        while time.time() < end_time:
            engine.poll_once(max_per_trader=100)

            # 중간 리포트
            if time.time() - last_report >= report_every * 60:
                engine.print_report()
                last_report = time.time()
                engine.save_result(output_path)  # 중간 저장

            remaining = end_time - time.time()
            if remaining > 0:
                sleep_secs = min(poll_interval_seconds, remaining)
                log.info(f"  💤 {sleep_secs:.0f}초 대기 | 잔여 {remaining/60:.1f}분")
                time.sleep(sleep_secs)

    except KeyboardInterrupt:
        log.info("⛔ 중단됨")

    engine.print_report(verbose=True)
    return engine.save_result(output_path)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Copy Perp Mainnet Papertrading v2")
    parser.add_argument("--duration", type=int, default=0,
                        help="실행 시간(분). 0=1회 즉시 실행 (기본)")
    parser.add_argument("--interval", type=int, default=120,
                        help="폴링 간격(초, 기본 120)")
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--no-lb", action="store_true",
                        help="리더보드 갱신 없이 기본 트레이더 사용")
    args = parser.parse_args()

    run_papertrading(
        duration_minutes=args.duration,
        poll_interval_seconds=args.interval,
        output_path=args.output,
        use_leaderboard=not args.no_lb,
    )
