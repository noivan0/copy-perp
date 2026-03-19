#!/usr/bin/env python3
"""
CopyPerp Verified PnL Report
mainnet 실데이터 기반 신뢰도 증명 리포트

실행: python3 demo/verified_pnl_report.py
"""

import json, sys, os, math
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core.verified_pnl import compute_verified_pnl, build_trust_report

C = {
    "green":   "\033[92m", "red":    "\033[91m", "yellow": "\033[93m",
    "cyan":    "\033[96m", "bold":   "\033[1m",  "dim":    "\033[2m",
    "reset":   "\033[0m",  "blue":   "\033[94m", "mag":    "\033[95m",
    "white":   "\033[97m",
}
def c(color, text): return f"{C[color]}{text}{C['reset']}"
def bar(val, max_val=100, width=16):
    filled = int(width * min(val, max_val) / max_val)
    return "█" * filled + "░" * (width - filled)

def load_crs_data():
    base = os.path.dirname(os.path.dirname(__file__))
    with open(os.path.join(base, "crs_result.json")) as f:
        data = json.load(f)
    return data.get("passed", [])


def print_section(title):
    print()
    print(c("bold", f"{'━'*64}"))
    print(c("bold", c("cyan", f"  {title}")))
    print(c("bold", f"{'━'*64}"))


def print_trust_header(report):
    conf = report["confidence"]
    icon = report["confidence_icon"]
    print()
    print(c("bold", "=" * 64))
    print(c("bold", c("cyan",
        "  ╔═══════════════════════════════════════╗\n"
        "  ║    CopyPerp  Verified PnL Report      ║\n"
        "  ║    mainnet 실데이터 기반 신뢰도 증명     ║\n"
        "  ╚═══════════════════════════════════════╝"
    )))
    conf_color = "green" if conf == "HIGH" else "yellow" if conf == "MEDIUM" else "red"
    print(f"\n  신뢰도 등급: {c(conf_color, c('bold', f'{icon} {conf}'))}")
    print(c("dim", "  데이터: Hyperliquid Mainnet API  |  날짜: 2026-03-16"))
    print(c("bold", "=" * 64))


def print_portfolio_summary(ps, capital):
    print_section("📊 포트폴리오 요약 (검증된 실적 기반)")

    net_pnl = ps["net_pnl_usdc"]
    net_roi = ps["net_roi_pct"]
    pnl_color = "green" if net_pnl >= 0 else "red"
    sign = "+" if net_pnl >= 0 else ""

    print(f"""
  초기 자본:    {c('cyan', f'${ps["capital_usdc"]:>12,.2f} USDC')}
  복사 비율:    {c('cyan', f'{ps["copy_ratio"]*100:.0f}%')}
  분석 기간:    {c('cyan', f'{ps["period_days"]}일')}
  트레이더 수:  {c('cyan', f'{ps["traders_count"]}명')}  (평균 CRS: {c('cyan', f'{ps["avg_crs"]:.1f}')})

  ┌─────────────────────────────────────────┐
  │  수익 내역                               │
  │                                         │""")

    print(f"  │  총 복사 수익     {c(pnl_color, c('bold', f'{sign}${ps[\"gross_pnl_usdc\"]:>10,.2f}')):<30}│")
    print(f"  │  수수료 차감       {c('red',  f'-${ps[\"total_fee_usdc\"]:>10,.2f}'):<30}│")
    print(f"  │  ─────────────────────────────────── │")
    print(f"  │  순 수익 (Net PnL) {c(pnl_color, c('bold', f'{sign}${net_pnl:>10,.2f}')):<30}│")
    print(f"  │  순 ROI            {c(pnl_color, c('bold', f'{sign}{net_roi:>9.2f}%')):<30}│")
    print(f"  │  최종 자산         {c('cyan', f'${ps[\"final_equity_usdc\"]:>10,.2f} USDC'):<30}│")
    print(f"  └─────────────────────────────────────────┘")

    print(f"""
  리스크 지표:
    Sharpe:      {c('cyan', f'{ps["sharpe_ratio"]:.2f}')}  {c('dim', '(1.0 이상 = 양호)')}
    추정 MaxDD:  {c('yellow', f'{ps["est_max_drawdown_pct"]:.1f}%')}
    몬테카를로:  {c('green', f'{ps["monte_carlo_survival_pct"]:.1f}%')} 생존율  {c('dim', '(1,000회 시뮬)')}
""")

    # 등급 분포 바
    print(f"  트레이더 등급 분포:")
    for grade, cnt in sorted(ps["grade_distribution"].items(), key=lambda x: {"S":4,"A":3,"B":2,"C":1,"D":0}.get(x[0],0), reverse=True):
        grade_color = "green" if grade in ("S","A") else "yellow" if grade == "B" else "red"
        label = {"S":"🏆 Elite","A":"⭐ Top","B":"✅ Qualified","C":"⚠️ Caution"}.get(grade, grade)
        print(f"    {c(grade_color, f'{label}'):<30} {c('bold', f'{cnt}명')}")


def print_per_trader(traders, capital, copy_ratio):
    print_section("🔍 트레이더별 검증 실적 (mainnet 온체인 데이터)")

    print(f"  {'트레이더':<12} {'등급':<10} {'CRS':>5} {'30d ROI':>8} {'트레이더PnL':>12} {'팔로워순익':>10} {'신뢰'}")
    print(c("dim", "  " + "─" * 70))

    for t in traders:
        grade_color = "green" if t["grade"] in ("S","A") else "yellow" if t["grade"] == "B" else "red"
        roi_str = c("green", f"+{t['trader_roi_30d_pct']:.1f}%") if t["trader_roi_30d_pct"] >= 0 else c("red", f"{t['trader_roi_30d_pct']:.1f}%")
        net_pnl = t["follower_net_pnl"]
        pnl_color = "green" if net_pnl >= 0 else "red"
        sign = "+" if net_pnl >= 0 else ""

        warn_icon = c("yellow", " ⚠") if t["warnings"] else c("green", " ✓")

        crs_bar = bar(t["crs"], 100, 8)
        crs_colored = c("green" if t["crs"] >= 80 else "yellow" if t["crs"] >= 65 else "red", crs_bar)

        print(
            f"  {c('cyan', t['alias']):<12} "
            f"{c(grade_color, t['grade_label']):<18} "
            f"{t['crs']:>5.1f} "
            f"{roi_str:>18} "
            f"{c('dim', f'${t[\"trader_pnl_30d_usdc\"]:>+,.0f}'):>22} "
            f"{c(pnl_color, f'{sign}${net_pnl:>7,.2f}'):>20}"
            f"{warn_icon}"
        )

        # 세부 점수 바 (접힘)
        if t["grade"] in ("S", "A"):
            print(f"    {c('dim', f'모멘텀 [{bar(t[\"momentum_score\"],100,8)}] {t[\"momentum_score\"]:.0f}  수익성 [{bar(t[\"profitability_score\"],100,8)}] {t[\"profitability_score\"]:.0f}  리스크 [{bar(t[\"risk_score\"],100,8)}] {t[\"risk_score\"]:.0f}')}")

        if t["warnings"]:
            for w in t["warnings"]:
                print(c("yellow", f"    ⚠  {w}"))

    print()
    print(c("dim", f"  * 팔로워 순익 = 트레이더ROI × 복사비율({copy_ratio*100:.0f}%) × 현실화계수(82%) − 수수료(0.15%/거래)"))
    print(c("dim",  "  * 데이터 출처: Hyperliquid Mainnet Leaderboard API (2026-03-16)"))


def print_benchmark(benchmark, capital):
    print_section("⚖️  수익 비교 ($10,000 투자, 30일 기준)")

    scenarios = [
        ("btc_hodl",          "red"),
        ("eth_hodl",          "red"),
        ("manual_trading",    "yellow"),
        ("copyperp_verified", "green"),
    ]

    max_abs_roi = max(abs(benchmark[k]["roi_pct"]) for k in benchmark)

    for key, color in scenarios:
        b = benchmark[key]
        roi = b["roi_pct"]
        pnl = b["pnl_usdc"]
        sign = "+" if roi >= 0 else ""
        roi_str = c(color, c("bold", f"{sign}{roi:.1f}%"))
        pnl_str = c(color, f"{sign}${pnl:,.0f}")

        bar_width = int(abs(roi) / max_abs_roi * 18)
        if roi >= 0:
            bar_str = c(color, "█" * bar_width + "░" * (18 - bar_width))
        else:
            bar_str = c(color, "▓" * bar_width + "░" * (18 - bar_width))

        sharpe_str = f"  Sharpe {b['sharpe']:.2f}" if "sharpe" in b else ""
        print(f"  {b['label']:<24} {bar_str} {roi_str} ({pnl_str}){sharpe_str}")
        print(c("dim", f"    {b['risk']}"))
        print()


def print_trust_basis(tb):
    print_section("🔒 신뢰도 근거 (공개 검증 가능)")

    print(f"""
  데이터 소스:  {c('cyan', tb['data_source'])}
  데이터 날짜:  {c('cyan', tb['data_date'])}
  평가 방법:    {c('cyan', tb['scoring_method'])}

  CRS 구성 지표:""")
    for k, v in tb["crs_components"].items():
        print(f"    {c('cyan', f'{k:<15}')} {v}")

    print(f"\n  현실화 보정:")
    for k, v in tb["realism_adjustments"].items():
        print(f"    {c('yellow', f'{k:<15}')} {v}")

    print(f"\n  등급 기준:")
    for grade, criteria in tb["grade_criteria"].items():
        grade_color = "green" if grade in ("S","A") else "yellow" if grade == "B" else "red"
        label = {"S":"🏆 Elite","A":"⭐ Top","B":"✅ Good","C":"⚠️ Caution"}.get(grade, grade)
        print(f"    {c(grade_color, f'{label}'):<22}  "
              f"CRS≥{criteria['min_crs']:.0f}  "
              f"ROI≥{criteria['min_roi_30d_pct']:.0f}%  "
              f"WR≥{criteria['min_win_rate']}  "
              f"DD≤{criteria['max_drawdown_pct']:.0f}%")


def print_disclaimers(disclaimers, proof_note):
    print_section("📌 주의사항 및 계산 근거")
    print(f"\n  계산 근거: {c('dim', proof_note)}\n")
    for d in disclaimers:
        print(c("dim", f"  • {d}"))


def main():
    traders_raw = load_crs_data()

    # 세 가지 시나리오
    scenarios = [
        {"label": "보수형 ($1,000 · S등급만)",  "capital": 1_000,  "ratio": 0.12, "min_grade": "S"},
        {"label": "균형형 ($5,000 · A등급+)",   "capital": 5_000,  "ratio": 0.15, "min_grade": "A"},
        {"label": "적극형 ($10,000 · B등급+)",  "capital": 10_000, "ratio": 0.18, "min_grade": "B"},
    ]

    all_reports = []
    for sc in scenarios:
        proof = compute_verified_pnl(
            traders_raw, capital=sc["capital"],
            copy_ratio=sc["ratio"], period_days=30,
            min_grade=sc["min_grade"]
        )
        report = build_trust_report(proof)
        all_reports.append((sc["label"], proof, report))

    # ── 메인 리포트 출력 (균형형 기준) ──
    _, proof_main, report_main = all_reports[1]
    print_trust_header(report_main)
    print_portfolio_summary(report_main["portfolio_summary"], proof_main.capital)
    print_per_trader(report_main["per_trader"], proof_main.capital, proof_main.copy_ratio)
    print_benchmark(report_main["benchmark"], proof_main.capital)

    # ── 시나리오 비교 ──
    print_section("📈 투자 시나리오 비교 (30일 기준)")
    print(f"\n  {'시나리오':<30} {'초기자본':>10} {'순수익':>12} {'ROI':>8} {'Sharpe':>8} {'신뢰도':>10}")
    print(c("dim", "  " + "─" * 72))

    for label, proof, report in all_reports:
        ps = report["portfolio_summary"]
        net = ps["net_pnl_usdc"]
        roi = ps["net_roi_pct"]
        sharpe = ps["sharpe_ratio"]
        conf = report["confidence"]
        sign = "+" if net >= 0 else ""
        pnl_color = "green" if net >= 0 else "red"
        conf_color = "green" if conf == "HIGH" else "yellow" if conf == "MEDIUM" else "red"

        print(
            f"  {label:<30} "
            f"${ps['capital_usdc']:>9,.0f} "
            f"{c(pnl_color, c('bold', f'{sign}${net:>9,.2f}')):>22} "
            f"{c(pnl_color, f'{sign}{roi:.2f}%'):>18} "
            f"{c('cyan', f'{sharpe:.2f}'):>18} "
            f"{c(conf_color, conf):>20}"
        )

    print_trust_basis(report_main["trust_basis"])
    print_disclaimers(report_main["disclaimers"], proof_main.proof_note)

    # JSON 저장
    output = {
        "generated_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
        "scenarios": [
            {
                "label": label,
                "capital": proof.capital,
                "copy_ratio": proof.copy_ratio,
                "net_pnl": proof.net_pnl,
                "net_roi_pct": proof.net_roi_pct,
                "sharpe": proof.portfolio_sharpe,
                "traders_count": len(proof.traders),
                "confidence": report["confidence"],
            }
            for label, proof, report in all_reports
        ],
        "main_report": all_reports[1][2],
    }

    out_path = os.path.join(os.path.dirname(__file__), "verified_pnl_report.json")
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n{c('green', '  ✓ JSON 리포트 저장:')} {out_path}")
    print()
    print(c("bold", "=" * 64))
    print(c("cyan", c("bold", "  CopyPerp — 신뢰도는 숫자로 증명한다")))
    print(c("dim",  "  copyperp.xyz  |  Hyperliquid  |  Pacifica 2026"))
    print(c("bold", "=" * 64))
    print()


if __name__ == "__main__":
    main()
