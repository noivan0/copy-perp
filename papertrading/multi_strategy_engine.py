"""
papertrading/multi_strategy_engine.py
4개 전략 병렬 페이퍼트레이딩 엔진

각 전략 독립 포트폴리오:
  safe        $10k | copy=5%  | max=$50   | 트레이더 3명
  default     $10k | copy=18% | max=$500  | 트레이더 4명 (몬테카를로 최적)
  balanced    $10k | copy=10% | max=$100  | 트레이더 5명
  aggressive  $10k | copy=15% | max=$500  | 트레이더 6명

데이터 저장:
  results/strategy_{name}/portfolio.json  — 누적 포트폴리오
  results/strategy_{name}/trades.jsonl    — 체결 이력 (append)
  results/comparison.json                 — 4개 전략 비교 요약
"""

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from papertrading.paper_engine import _cf_get, get_leaderboard

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ── 디렉토리 설정 ──────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(__file__)
RESULTS_DIR = os.path.join(BASE_DIR, "results")
os.makedirs(RESULTS_DIR, exist_ok=True)

INITIAL_CAPITAL = 10_000.0
MIN_ORDER_USDC  = 0.5
REALISM_FACTOR  = 0.82   # 슬리피지 + 지연 보정
TAKER_FEE       = 0.0005 # 0.05%
BUILDER_FEE     = 0.001  # 0.10%
TOTAL_FEE       = TAKER_FEE + BUILDER_FEE

# ── 스노우볼 설정 ──────────────────────────────────────────────────────────────
# 자본이 증가할수록 copy_ratio를 비례 상향 → 복리 효과
# capital > SNOWBALL_BASE → ratio *= (capital / SNOWBALL_BASE) ^ SNOWBALL_EXP
SNOWBALL_BASE   = 10_000.0   # 기준 자본
SNOWBALL_EXP    = 0.5        # 지수 (0.5 = 완만한 복리, 1.0 = 완전 선형)
SNOWBALL_CAP    = 2.0        # 최대 2배 상향 (리스크 제한)

# ── 4가지 전략 프리셋 (531e → BkUTkCt4/FMhCxyGk 교체, 2026-03-19) ──────────
#
# 531e 교체 이유:
#   최근 100건 BTC 숏 편향, 승률 0%, PnL -$19,900
#   → BkUTkCt4 (WR 100%, PnL +$1,281, SOL/SUI/WLFI 다변화)
#   → FMhCxyGk (WR 62%, PnL +$1,194, SOL 중심)
STRATEGIES = {
    "safe": {
        "label":       "🛡 안전형",
        "copy_ratio":  0.05,
        "max_pos":     50.0,
        "stop_loss":   0.08,   # 8%
        "take_profit": 0.15,   # 15%
        "traders": [
            {"address": "FN4seJZ9Wdi3NCbugCkPD5xYac5UrCQmzQt4o3Ko5VB2", "alias": "FN4s",  "weight": 0.40},
            {"address": "49R9MFU7JopaCFXtpTwbaX8rkNW9wX6ddi7VtLUtMYJ1", "alias": "49R9",  "weight": 0.35},
            {"address": "Ph9yECGodDAjiiSU9bpbJ8dds3ndWP1ngKo8h1K2QYv",  "alias": "Ph9y",  "weight": 0.25},
        ],
        "expected_monthly_roi": 1.9,
    },
    "default": {
        "label":       "⚙️ 기본형",
        "copy_ratio":  0.18,
        "max_pos":     500.0,
        "stop_loss":   0.12,   # 12%
        "take_profit": 0.25,   # 25%
        "traders": [
            {"address": "Ph9yECGodDAjiiSU9bpbJ8dds3ndWP1ngKo8h1K2QYv",  "alias": "Ph9y",  "weight": 0.30},
            {"address": "FN4seJZ9Wdi3NCbugCkPD5xYac5UrCQmzQt4o3Ko5VB2", "alias": "FN4s",  "weight": 0.25},
            {"address": "BkUTkCt4JwQQwczibKkP5TEjTCHkSogR44ppvQReTt5B", "alias": "BkUT",  "weight": 0.25},  # 531e 대체: WR 100%
            {"address": "49R9MFU7JopaCFXtpTwbaX8rkNW9wX6ddi7VtLUtMYJ1", "alias": "49R9",  "weight": 0.20},
        ],
        "expected_monthly_roi": 10.42,
    },
    "balanced": {
        "label":       "⚖️ 균형형",
        "copy_ratio":  0.10,
        "max_pos":     100.0,
        "stop_loss":   0.10,   # 10%
        "take_profit": 0.22,   # 22%
        "traders": [
            {"address": "FN4seJZ9Wdi3NCbugCkPD5xYac5UrCQmzQt4o3Ko5VB2", "alias": "FN4s",  "weight": 0.25},
            {"address": "Ph9yECGodDAjiiSU9bpbJ8dds3ndWP1ngKo8h1K2QYv",  "alias": "Ph9y",  "weight": 0.25},
            {"address": "FMhCxyGkfcFEyr7mRQ9qpGGqzk3BwvgXxQcCXunfwpA", "alias": "FMhC",  "weight": 0.20},  # 531e 대체: WR 62%
            {"address": "DQqre2oHthtWYBFfJYJWPKBFDkMbCR5gJWjHUExqwTmq", "alias": "DQqr",  "weight": 0.15},
            {"address": "49R9MFU7JopaCFXtpTwbaX8rkNW9wX6ddi7VtLUtMYJ1", "alias": "49R9",  "weight": 0.15},
        ],
        "expected_monthly_roi": 2.7,
    },
    "aggressive": {
        "label":       "⚡ 공격형",
        "copy_ratio":  0.15,
        "max_pos":     500.0,
        "stop_loss":   0.12,   # 12%
        "take_profit": 0.30,   # 30%
        "traders": [
            {"address": "Ph9yECGodDAjiiSU9bpbJ8dds3ndWP1ngKo8h1K2QYv",  "alias": "Ph9y",  "weight": 0.25},
            {"address": "FN4seJZ9Wdi3NCbugCkPD5xYac5UrCQmzQt4o3Ko5VB2", "alias": "FN4s",  "weight": 0.20},
            {"address": "6uC2TdJxxqhWMPSjs7u9YE5rWMQs1yhxkvk8BmBTPrpV", "alias": "6uC2",  "weight": 0.20},
            {"address": "BkUTkCt4JwQQwczibKkP5TEjTCHkSogR44ppvQReTt5B", "alias": "BkUT",  "weight": 0.15},  # 531e 대체: WR 100%
            {"address": "FMhCxyGkfcFEyr7mRQ9qpGGqzk3BwvgXxQcCXunfwpA", "alias": "FMhC",  "weight": 0.10},  # 추가
            {"address": "DQqre2oHthtWYBFfJYJWPKBFDkMbCR5gJWjHUExqwTmq", "alias": "DQqr",  "weight": 0.10},
        ],
        "expected_monthly_roi": 4.1,
    },
}


# ── 포트폴리오 데이터클래스 ────────────────────────────────────────────────────
@dataclass
class StrategyPortfolio:
    strategy:        str
    label:           str
    capital:         float = INITIAL_CAPITAL
    peak_capital:    float = INITIAL_CAPITAL
    total_trades:    int   = 0
    wins:            int   = 0
    losses:          int   = 0
    gross_profit:    float = 0.0
    gross_loss:      float = 0.0
    total_fees:      float = 0.0
    max_dd_pct:      float = 0.0
    started_at:      str   = ""
    updated_at:      str   = ""
    session_count:   int   = 0
    trade_log:       list  = field(default_factory=list)  # 최근 50건
    equity_series:   list  = field(default_factory=list)  # equity 시계열

    @property
    def total_pnl(self) -> float:
        return self.capital - INITIAL_CAPITAL

    @property
    def roi_pct(self) -> float:
        return self.total_pnl / INITIAL_CAPITAL * 100

    @property
    def win_rate(self) -> float:
        n = self.wins + self.losses
        return self.wins / n * 100 if n > 0 else 0.0

    @property
    def profit_factor(self) -> float:
        return self.gross_profit / self.gross_loss if self.gross_loss > 0 else 9.99

    @property
    def net_pnl(self) -> float:
        return self.total_pnl - self.total_fees

    def update_dd(self):
        if self.capital > self.peak_capital:
            self.peak_capital = self.capital
        if self.peak_capital > 0:
            dd = (self.peak_capital - self.capital) / self.peak_capital * 100
            if dd > self.max_dd_pct:
                self.max_dd_pct = dd

    def to_summary(self) -> dict:
        return {
            "strategy":      self.strategy,
            "label":         self.label,
            "capital":       round(self.capital, 4),
            "total_pnl":     round(self.total_pnl, 4),
            "net_pnl":       round(self.net_pnl, 4),
            "roi_pct":       round(self.roi_pct, 4),
            "total_trades":  self.total_trades,
            "win_rate":      round(self.win_rate, 2),
            "profit_factor": round(self.profit_factor, 3),
            "max_dd_pct":    round(self.max_dd_pct, 3),
            "total_fees":    round(self.total_fees, 4),
            "session_count": self.session_count,
            "started_at":    self.started_at,
            "updated_at":    self.updated_at,
        }


# ── 포트폴리오 저장/로딩 ──────────────────────────────────────────────────────

def _strategy_dir(name: str) -> str:
    d = os.path.join(RESULTS_DIR, f"strategy_{name}")
    os.makedirs(d, exist_ok=True)
    return d


def load_portfolio(name: str) -> StrategyPortfolio:
    path = os.path.join(_strategy_dir(name), "portfolio.json")
    cfg  = STRATEGIES[name]
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
        p = StrategyPortfolio(strategy=name, label=cfg["label"])
        for k, v in data.items():
            if hasattr(p, k) and k not in ("roi_pct", "win_rate", "profit_factor", "total_pnl", "net_pnl"):
                setattr(p, k, v)
        return p
    p = StrategyPortfolio(strategy=name, label=cfg["label"])
    p.started_at = datetime.now(timezone.utc).isoformat()
    return p


def save_portfolio(p: StrategyPortfolio):
    path = os.path.join(_strategy_dir(p.strategy), "portfolio.json")
    data = asdict(p)
    data["roi_pct"]       = round(p.roi_pct, 4)
    data["win_rate"]      = round(p.win_rate, 2)
    data["profit_factor"] = round(p.profit_factor, 3)
    data["total_pnl"]     = round(p.total_pnl, 4)
    data["net_pnl"]       = round(p.net_pnl, 4)
    # trade_log 최근 200건만 보관
    data["trade_log"]     = p.trade_log[-200:]
    # equity_series 최근 1000건
    data["equity_series"] = p.equity_series[-1000:]
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def append_trade(name: str, trade_record: dict):
    path = os.path.join(_strategy_dir(name), "trades.jsonl")
    with open(path, "a") as f:
        f.write(json.dumps(trade_record, ensure_ascii=False) + "\n")


# ── 단일 전략 트레이딩 엔진 ───────────────────────────────────────────────────

class StrategyEngine:
    def __init__(self, name: str):
        self.name      = name
        self.cfg       = STRATEGIES[name]
        self.portfolio = load_portfolio(name)
        self.seen_ids: set = set()

        # 기존 seen_ids 복원 (중복 처리 방지)
        log_path = os.path.join(_strategy_dir(name), "trades.jsonl")
        if os.path.exists(log_path):
            with open(log_path) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                        if r.get("history_id"):
                            self.seen_ids.add(r["history_id"])
                    except Exception:
                        pass

    def _fetch_equity(self, address: str) -> float:
        lb = get_leaderboard(100)
        for row in lb:
            if row.get("address") == address:
                try: return float(row.get("equity_current", 0) or 0)
                except: return 0.0
        return 0.0

    def _fetch_trades(self, address: str, limit: int = 50) -> list:
        r = _cf_get(f"trades/history?account={address}&limit={limit}")
        if isinstance(r, dict) and r.get("success"):
            return r.get("data", []) or []
        return []

    def process_trade(self, trader: dict, trade: dict, equity: float) -> Optional[dict]:
        """단건 처리 → 포트폴리오 업데이트. 처리된 경우 record 반환."""
        hist_id = trade.get("history_id")
        if hist_id and hist_id in self.seen_ids:
            return None
        if hist_id:
            self.seen_ids.add(hist_id)

        side  = trade.get("side", "")
        cause = trade.get("cause", "normal")

        # open 포지션, 청산은 스킵
        if "open" in side or cause == "liquidation":
            return None

        pnl_raw = trade.get("pnl")
        if pnl_raw is None:
            return None
        try:
            raw_pnl = float(pnl_raw)
        except:
            return None

        amount      = float(trade.get("amount", 0) or 0)
        price       = float(trade.get("price", 0) or 0)
        entry_price = float(trade.get("entry_price", 0) or 0)
        if amount <= 0:
            return None

        pos_usdc = amount * (entry_price if entry_price > 0 else price)
        if pos_usdc <= 0:
            return None

        cfg = self.cfg
        # ── 스노우볼: 현재 자본 기반 copy_ratio 동적 상향 ────────────────────
        # 자본이 초기 대비 늘어날수록 더 많이 복사 → 복리 효과
        cur_capital = self.portfolio.capital
        if cur_capital > SNOWBALL_BASE:
            snowball_mult = min(SNOWBALL_CAP, (cur_capital / SNOWBALL_BASE) ** SNOWBALL_EXP)
        else:
            snowball_mult = 1.0   # 자본 감소 시 그대로 유지 (리스크 보호)

        # 비율 계산 (스노우볼 적용)
        base_ratio = cfg["copy_ratio"] * trader["weight"] * snowball_mult
        scale      = min(1.0, INITIAL_CAPITAL / equity) if equity > 0 else 1.0
        ratio      = base_ratio * scale

        # max_pos 클램핑 (스노우볼 적용: max_pos도 비례 확대)
        effective_max = cfg["max_pos"] * snowball_mult
        capped_pos = min(pos_usdc * ratio, effective_max)
        if pos_usdc * ratio > 0:
            ratio = capped_pos / pos_usdc

        copy_pnl = raw_pnl * ratio * REALISM_FACTOR

        # SL/TP 적용 (포지션 크기 기준)
        if copy_pnl < -capped_pos * cfg["stop_loss"]:
            copy_pnl = -capped_pos * cfg["stop_loss"]
        if copy_pnl > capped_pos * cfg["take_profit"]:
            copy_pnl = capped_pos * cfg["take_profit"]

        # 최소 주문 필터
        if capped_pos < MIN_ORDER_USDC:
            return None

        # 수수료
        fee = capped_pos * TOTAL_FEE

        net = copy_pnl - fee
        self.portfolio.capital    += net
        self.portfolio.total_fees += fee
        self.portfolio.total_trades += 1
        if copy_pnl > 0:
            self.portfolio.wins        += 1
            self.portfolio.gross_profit += copy_pnl
        else:
            self.portfolio.losses      += 1
            self.portfolio.gross_loss  += abs(copy_pnl)

        self.portfolio.update_dd()
        self.portfolio.equity_series.append(round(self.portfolio.capital, 4))
        self.portfolio.updated_at = datetime.now(timezone.utc).isoformat()

        record = {
            "ts":              int(time.time() * 1000),
            "dt":              datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "history_id":      hist_id,
            "strategy":        self.name,
            "trader":          trader["alias"],
            "symbol":          trade.get("symbol", "?"),
            "side":            side,
            "pos_usdc":        round(capped_pos, 4),
            "raw_pnl":         round(raw_pnl, 4),
            "copy_pnl":        round(copy_pnl, 4),
            "fee":             round(fee, 4),
            "net":             round(net, 4),
            "ratio":           round(ratio, 6),
            "snowball_mult":   round(snowball_mult, 4),   # 스노우볼 배율
            "capital":         round(self.portfolio.capital, 4),
            "roi_pct":         round(self.portfolio.roi_pct, 4),
        }
        self.portfolio.trade_log.append(record)
        return record

    def run_poll(self) -> dict:
        """1회 폴링: 모든 트레이더 최신 체결 확인 → 처리"""
        new_trades = []
        for trader in self.cfg["traders"]:
            addr   = trader["address"]
            equity = self._fetch_equity(addr)
            trades = self._fetch_trades(addr, limit=20)
            for trade in trades:
                rec = self.process_trade(trader, trade, equity)
                if rec:
                    new_trades.append(rec)
                    append_trade(self.name, rec)
                    log.info(
                        f"[{self.name:>10}] {trader['alias']} "
                        f"{rec['symbol']:>6} {rec['side']:>15} "
                        f"net={rec['net']:+.4f}  "
                        f"capital=${self.portfolio.capital:,.2f} "
                        f"ROI={self.portfolio.roi_pct:+.3f}%"
                    )
            time.sleep(0.3)  # rate limit

        self.portfolio.session_count += 1 if not new_trades else 0
        save_portfolio(self.portfolio)
        return {"strategy": self.name, "new_trades": len(new_trades)}


# ── 비교 리포트 ───────────────────────────────────────────────────────────────

def build_comparison() -> dict:
    summaries = []
    for name in STRATEGIES:
        p = load_portfolio(name)
        s = p.to_summary()
        s["expected_monthly_roi"] = STRATEGIES[name]["expected_monthly_roi"]
        s["trader_count"]         = len(STRATEGIES[name]["traders"])
        summaries.append(s)

    # ROI 기준 랭킹
    summaries.sort(key=lambda x: x["roi_pct"], reverse=True)
    for i, s in enumerate(summaries):
        s["rank"] = i + 1

    comparison = {
        "generated_at":   datetime.now(timezone.utc).isoformat(),
        "initial_capital": INITIAL_CAPITAL,
        "strategies":     summaries,
        "best_strategy":  summaries[0]["strategy"] if summaries else None,
        "note":           "mainnet 실데이터 기반 페이퍼트레이딩 (슬리피지 18% 반영, 수수료 0.15%/trade)",
    }

    path = os.path.join(RESULTS_DIR, "comparison.json")
    with open(path, "w") as f:
        json.dump(comparison, f, indent=2, ensure_ascii=False)
    return comparison


def print_comparison(comparison: dict):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    C = {
        "g": "\033[92m", "r": "\033[91m", "y": "\033[93m",
        "c": "\033[96m", "b": "\033[1m",  "d": "\033[2m",
        "x": "\033[0m",
    }
    def c(col, txt): return f"{C[col]}{txt}{C['x']}"
    def bar(v, mx=5, w=12):
        filled = int(w * min(abs(v), mx) / mx)
        sym = "█" if v >= 0 else "▓"
        col = "g" if v >= 0 else "r"
        return c(col, sym * filled + "░" * (w - filled))

    print(f"\n{c('b', '='*68)}")
    print(c("b", c("c", "  CopyPerp 4-Strategy Paper Trading Comparison")))
    print(c("d", f"  {now} | $10,000 기준 | mainnet 실데이터"))
    print(c("b", "="*68))
    print()
    print(f"  {'#':>2}  {'전략':>12}  {'자본':>12}  {'ROI':>8}  {'거래':>5}  {'승률':>7}  {'MaxDD':>7}  {'예상월ROI':>9}")
    print(c("d", "  " + "-"*64))

    for s in comparison.get("strategies", []):
        rank   = s["rank"]
        label  = s["label"]
        cap    = s["capital"]
        roi    = s["roi_pct"]
        trades = s["total_trades"]
        wr     = s["win_rate"]
        dd     = s["max_dd_pct"]
        exp    = s["expected_monthly_roi"]

        roi_col  = "g" if roi >= 0 else "r"
        roi_sign = "+" if roi >= 0 else ""
        wr_col   = "g" if wr >= 50 else "y" if wr >= 40 else "r"
        rank_sym = ["🥇", "🥈", "🥉", "4️⃣"][rank-1] if rank <= 4 else str(rank)

        print(
            f"  {rank_sym}  {c('c', label):<22} "
            f"{c('b', f'${cap:,.2f}'):>22}  "
            f"{c(roi_col, f'{roi_sign}{roi:.3f}%'):>18}  "
            f"{trades:>5}  "
            f"{c(wr_col, f'{wr:.1f}%'):>17}  "
            f"{c('y', f'{dd:.2f}%'):>17}  "
            f"{c('d', f'+{exp}%/월'):>9}"
        )

    print()
    best = comparison.get("best_strategy", "")
    if best:
        print(c("g", c("b", f"  ✅ 현재 최우수 전략: {best}")))
    print()
    print(c("d", f"  누적 데이터 위치: papertrading/results/"))
    print(c("b", "="*68 + "\n"))


# ── 메인 실행 ────────────────────────────────────────────────────────────────

def run_all_strategies(poll_interval: int = 120):
    """4개 전략 순차 폴링 (각 2분 간격)"""
    engines = {name: StrategyEngine(name) for name in STRATEGIES}

    log.info("=" * 60)
    log.info("  CopyPerp Multi-Strategy Paper Trading 시작")
    log.info(f"  전략: {list(STRATEGIES.keys())}")
    log.info(f"  폴링 간격: {poll_interval}초")
    log.info(f"  초기 자본: ${INITIAL_CAPITAL:,}")
    log.info("=" * 60)

    poll_num = 0
    while True:
        poll_num += 1
        poll_start = time.time()

        log.info(f"\n--- Poll #{poll_num} @ {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')} ---")

        total_new = 0
        for name, engine in engines.items():
            try:
                result = engine.run_poll()
                total_new += result["new_trades"]
            except Exception as e:
                log.error(f"[{name}] 폴링 오류: {e}")

        # 비교 리포트 갱신
        try:
            comparison = build_comparison()
            if poll_num % 5 == 1:  # 5번마다 콘솔 출력
                print_comparison(comparison)
            else:
                # 간략 요약
                strats = comparison.get("strategies", [])
                parts = [f"{s['label']} {'+' if s['roi_pct']>=0 else ''}{s['roi_pct']:.3f}%" for s in strats]
                log.info(f"  요약: {' | '.join(parts)} | 신규체결: {total_new}건")
        except Exception as e:
            log.error(f"비교 리포트 오류: {e}")

        elapsed = time.time() - poll_start
        wait    = max(0, poll_interval - elapsed)
        if wait > 0:
            log.info(f"  다음 폴링까지 {wait:.0f}초 대기...")
            time.sleep(wait)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=120, help="폴링 간격 (초)")
    parser.add_argument("--report",   action="store_true", help="현재 상태 리포트만 출력")
    args = parser.parse_args()

    if args.report:
        comp = build_comparison()
        print_comparison(comp)
    else:
        run_all_strategies(poll_interval=args.interval)
