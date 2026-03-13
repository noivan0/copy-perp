#!/usr/bin/env python3
"""
Copy Perp 백테스팅 엔진 v2
- 진입 지연(1초) 슬리피지 반영
- copy_ratio 0.05/0.10/0.20 시나리오 비교
- 트레이더 Tier 차등 배분 가중치

Usage:
    python3 scripts/backtest.py [--capital 10000] [--slippage 0.001]
"""
import sys, json, time, argparse
sys.path.insert(0, '.')
from dotenv import load_dotenv; load_dotenv('.env')
from pacifica.client import _cf_request
from core.stats import compute_trader_stats

# Tier 1 — 리서치팀 확정 + 팀별 분석 통합
TIER1_TRADERS = [
    ("EcX5xSDT45Nvhi2gMTjTnhF3KT2w4sPF54esEZS3hwZu", 0.30, "TIER1"),
    ("4UBH19qUbXEaqyz9fKrFHuvj8BPMoM87H71s1YPKyGYq",  0.20, "TIER1"),
    ("A6VY4ZBUohgSLkwMuDwDvAnzgiXFB1eTDzaixyitPJep",  0.10, "TIER1"),
    ("7C3sXQ6KvXJLkYGwzjNy2BHpkfEnRHzzfVAgUS64CDEd",  0.10, "TIER1"),
    ("7gV81bz99MUBVb2aLYxW7MG1RMDdRdJYTPyC2syjba8y",  0.08, "TIER1"),
    ("3rXoG6i55P7D1Q3tYsB7Unds8nBtKh7vH5VUyMDpWkSe",  0.08, "TIER1"),
    ("EYhhf8u9M6kN9tCRVgd2Jki9fJm3XzJRnTF9k5eBC1q1",  0.05, "TIER1"),
    ("FuHMGqdrn77u944FSYvg9VTw3sD5RVeYS1ezLpGaFes7",  0.05, "TIER1"),
    ("E1vabqxiuUfB29BAwLppTLLNMAq6HJqp7gSz1NiYwWz7",  0.04, "TIER2"),
    ("5BPd5WYVvDE2kHMjzGmLHMaAorSm8bEfERcsycg5GCAD",  0.00, "TIER2"),
    ("7kDTQZPTnaCidXZwEhkoLSia5BKb7zhQ6CmBX2g1RiG3",  0.00, "TIER2"),
    ("8r5HRJeSScGX1TB9D2FZ45xEDspm1qfK4CTuaZvqe7en",  0.00, "TIER2"),
    ("A4XbPsH59TWjp6vx3QnY8sCb26ew4pBYkYc8Vk4kpbqk",  0.00, "TIER2"),
    ("DThxt2yhDvJv9KU9bPMuKsd7vcwdDtaRtuh4NvohutQi",  0.00, "TIER2"),
    ("AF5a28meHjecM4dNy8FssFHquWJVv4BK1e5Z8ipRkDgT",   0.00, "TIER2"),
]


def fetch_trades(addr: str) -> list:
    try:
        r = _cf_request("GET", f"trades/history?account={addr}&limit=100")
        trades = r.get("data", r) if isinstance(r, dict) else r
        return trades if isinstance(trades, list) else []
    except Exception as e:
        return []


def backtest_single(trades: list, capital: float, ratio: float,
                    weight: float, slippage: float = 0.001,
                    entry_delay_ms: int = 1000) -> dict:
    """단일 트레이더 백테스팅 (슬리피지 + 지연 포함)"""
    equity = capital * weight  # 가중치 배분 자본
    peak = equity
    max_dd = 0.0
    wins = losses = 0
    gross_profit = gross_loss = 0.0

    for tr in reversed(trades):
        pnl = float(tr.get("pnl", 0) or 0)
        if pnl == 0:
            continue
        # 진입 지연 슬리피지 반영 (1초 지연 → entry_delay_ms/1000 초 동안 불리하게)
        # 평균 슬리피지: slippage * |pnl|
        slip_cost = abs(pnl) * slippage
        copy_pnl = pnl * ratio - slip_cost
        equity += copy_pnl
        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)
        if copy_pnl > 0:
            wins += 1
            gross_profit += copy_pnl
        else:
            losses += 1
            gross_loss += abs(copy_pnl)

    total = wins + losses
    roi = (equity - capital * weight) / (capital * weight) * 100 if (capital * weight) > 0 else 0
    pf = gross_profit / gross_loss if gross_loss > 0 else 999.0
    return {
        "equity": equity, "roi": roi, "max_dd": max_dd,
        "win_rate": wins / total * 100 if total > 0 else 0,
        "pf": pf, "trades": total, "weight": weight,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--capital", type=float, default=10000)
    parser.add_argument("--slippage", type=float, default=0.001, help="슬리피지 비율 (기본 0.1%)")
    args = parser.parse_args()

    SCENARIOS = [0.05, 0.10, 0.20]  # copy_ratio 3가지

    print("=" * 75)
    print(f"Copy Perp 백테스팅 v2 | 자본 ${args.capital:,} | 슬리피지 {args.slippage*100:.2f}%")
    print("=" * 75)

    # 거래 내역 수집
    all_trades: dict[str, list] = {}
    active_traders = [(a, w, t) for a, w, t in TIER1_TRADERS if w > 0]
    print(f"\n📡 거래내역 수집 ({len(active_traders)}명)...")
    for addr, weight, tier in active_traders:
        trades = fetch_trades(addr)
        all_trades[addr] = trades
        stats = compute_trader_stats(trades)
        print(f"  {addr[:16]}... {len(trades):3}건 | WR={stats['win_rate']:.0f}% PF={stats['profit_factor']:.1f}x [{tier} w={weight}]")
        time.sleep(0.3)

    # 시나리오별 백테스팅
    print(f"\n{'':=<75}")
    print("📊 시나리오 비교 (copy_ratio 별)")
    print(f"{'':=<75}")
    print(f"{'시나리오':^10} | {'최종자산':^12} | {'총ROI':^8} | {'MaxDD':^7} | {'포트폴리오 기여'}") 
    print("-" * 75)

    scenario_results = {}
    for ratio in SCENARIOS:
        portfolio_equity = 0.0
        portfolio_peak = args.capital
        portfolio_dd = 0.0
        contrib = []
        for addr, weight, tier in active_traders:
            trades = all_trades.get(addr, [])
            if not trades:
                continue
            r = backtest_single(trades, args.capital, ratio, weight, args.slippage)
            portfolio_equity += r["equity"]
            contrib.append((addr, weight, tier, r["roi"]))

        roi = (portfolio_equity - args.capital) / args.capital * 100
        scenario_results[ratio] = {"equity": portfolio_equity, "roi": roi, "contrib": contrib}
        label = f"ratio={ratio:.0%}"
        top = sorted(contrib, key=lambda x: -x[3])[:3]
        top_str = " | ".join(f"{a[:8]}…{r:.1f}%" for a,_,_,r in top)
        print(f"  {label:10} | ${portfolio_equity:>10,.0f} | {roi:>6.1f}%  | — | {top_str}")

    # 추천 시나리오
    best_ratio = max(SCENARIOS, key=lambda r: scenario_results[r]["roi"])
    best = scenario_results[best_ratio]
    print(f"\n{'':=<75}")
    print(f"🏆 최적 시나리오: copy_ratio={best_ratio:.0%}")
    print(f"   ${args.capital:,} → ${best['equity']:,.0f} | ROI {best['roi']:.1f}%")

    # 트레이더별 기여도
    print(f"\n📊 트레이더별 기여도 (ratio={best_ratio:.0%})")
    print(f"  {'주소':18} {'Tier':6} {'가중치':6} {'ROI':8} {'기여순위'}")
    print("  " + "-" * 55)
    sorted_c = sorted(best["contrib"], key=lambda x: -x[3])
    medals = ["🥇","🥈","🥉"] + ["  "] * 20
    for i, (addr, weight, tier, roi) in enumerate(sorted_c):
        print(f"  {medals[i]} {addr[:16]}… [{tier}] w={weight:.2f} ROI={roi:>6.1f}%")

    print(f"\n권장: python3 scripts/register_traders.py --host http://localhost:8001")


if __name__ == "__main__":
    main()
