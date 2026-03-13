#!/usr/bin/env python3
"""
Copy Perp 백테스팅 스크립트
과거 거래 내역 기반 Copy Trading 수익 시뮬레이션

사용법:
    python3 scripts/backtest.py [--ratio 0.1] [--capital 10000] [--max-per-trade 500]
"""
import sys, json, time, argparse
sys.path.insert(0, '.')
from dotenv import load_dotenv
load_dotenv('.env')
from pacifica.client import _cf_request

TRADERS = [
    ("EcX5xSDT45Nvhi2gMTjTnhF3KT2w4sPF54esEZS3hwZu", "ROI#1-82.5%"),
    ("4UBH19qUbXEaqyz9fKrFHuvj8BPMoM87H71s1YPKyGYq",  "ROI#2-68.8%"),
    ("A6VY4ZBUohgSLkwMuDwDvAnzgiXFB1eTDzaixyitPJep",  "ROI#3-60.2%"),
    ("7C3sXQ6KvXJLkYGwzjNy2BHpkfEnRHzzfVAgUS64CDEd",  "ROI#4-59.7%"),
    ("7gV81bz99MUBVb2aLYxW7MG1RMDdRdJYTPyC2syjba8y",  "ROI#5-55.1%"),
    ("FuHMGqdrn77u944FSYvg9VTw3sD5RVeYS1ezLpGaFes7",  "Win88%+PF18"),
    ("EYhhf8u9M6kN9tCRVgd2Jki9fJm3XzJRnTF9k5eBC1q1",  "PF-1000"),
    ("3rXoG6i55P7D1Q3tYsB7Unds8nBtKh7vH5VUyMDpWkSe",  "ROI-42%"),
    ("E1vabqxiuUfB29BAwLppTLLNMAq6HJqp7gSz1NiYwWz7",  "ROI-42%b"),
    ("AF5a28meHjecM4dNy8FssFHquWJVv4BK1e5Z8ipRkDgT",   "ROI-44%"),
]


def run_backtest(addr: str, label: str, capital: float, ratio: float, max_per_trade: float):
    try:
        r = _cf_request("GET", f"trades/history?account={addr}&limit=100")
        trades = r.get("data", []) if isinstance(r, dict) else r
        if not isinstance(trades, list) or not trades:
            return None
    except Exception as e:
        print(f"  {label}: 조회 실패 — {e}")
        return None

    equity = capital
    peak = equity
    max_dd = 0.0
    wins = losses = 0
    gross_profit = gross_loss = 0.0

    for tr in reversed(trades):
        pnl = float(tr.get("pnl", 0))
        if pnl == 0:
            continue
        copy_pnl = min(abs(pnl) * ratio, max_per_trade)
        copy_pnl = copy_pnl if pnl > 0 else -copy_pnl
        equity += copy_pnl
        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100
        max_dd = max(max_dd, dd)
        if copy_pnl > 0:
            wins += 1
            gross_profit += copy_pnl
        else:
            losses += 1
            gross_loss += abs(copy_pnl)

    total = wins + losses
    winrate = wins / total * 100 if total > 0 else 0
    roi = (equity - capital) / capital * 100
    pf = gross_profit / gross_loss if gross_loss > 0 else 999.0

    return {
        "addr": addr, "label": label,
        "final": equity, "roi": roi, "max_dd": max_dd,
        "winrate": winrate, "pf": pf,
        "wins": wins, "losses": losses,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ratio", type=float, default=0.1)
    parser.add_argument("--capital", type=float, default=10000)
    parser.add_argument("--max-per-trade", type=float, default=500)
    args = parser.parse_args()

    print("=" * 70)
    print(f"Copy Perp 백테스팅 | 자본 ${args.capital:,} | 복사비율 {args.ratio*100:.0f}% | 건당최대 ${args.max_per_trade}")
    print("=" * 70)

    results = []
    for addr, label in TRADERS:
        r = run_backtest(addr, label, args.capital, args.ratio, args.max_per_trade)
        if r:
            results.append(r)
        time.sleep(0.3)

    # Risk-Adjusted: ROI - MaxDD*0.3
    results.sort(key=lambda x: -(x['roi'] - x['max_dd'] * 0.3))

    print(f"\n{'':2} {'레이블':16} {'최종자산':>10} {'ROI':>7} {'MaxDD':>7} {'Win률':>7} {'PF':>6} {'거래':>5}")
    print("-" * 70)
    medals = ["🥇", "🥈", "🥉"] + ["  "] * 20
    for i, r in enumerate(results):
        print(f"{medals[i]} {r['label']:14} ${r['final']:>9,.0f} {r['roi']:>6.1f}% {r['max_dd']:>6.1f}% "
              f"{r['winrate']:>6.1f}% {r['pf']:>5.1f}x {r['wins']+r['losses']:>4}")

    print("\n" + "=" * 70)
    print("최적 팔로우 전략 TOP 3")
    print("=" * 70)
    for i, r in enumerate(results[:3]):
        profit = r['final'] - args.capital
        print(f"\n#{i+1} [{r['label']}]")
        print(f"   주소: {r['addr']}")
        print(f"   ROI: {r['roi']:.1f}% | MaxDD: {r['max_dd']:.1f}% | Win: {r['winrate']:.0f}% | PF: {r['pf']:.2f}x")
        print(f"   ${args.capital:,} 복사 → 예상 수익: ${profit:,.0f}")

    print(f"\n권장 설정: copy_ratio={args.ratio}, max_per_trade=${args.max_per_trade}")


if __name__ == "__main__":
    main()
