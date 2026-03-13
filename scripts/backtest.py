"""
Copy Perp 백테스팅 스크립트
실제 Pacifica 리더보드 데이터로 팔로우 전략 시뮬레이션

실행: python3 scripts/backtest.py
"""

import os, sys, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from pacifica.client import _proxy_get
import warnings
warnings.filterwarnings("ignore")


# ── 설정 ────────────────────────────────────────────
INITIAL_CAPITAL = 10_000.0   # 팔로워 초기 자금 ($)
COPY_RATIO      = 0.10       # 복사 비율 (10%)
MAX_PER_TRADER  = 0.30       # 단일 트레이더 최대 비중 (30%)
MIN_TIER1_PNL30 = 50_000     # Tier1: 30d PnL 기준
MIN_TIER2_PNL30 = 10_000     # Tier2: 30d PnL 기준
COPY_DELAY_SEC  = 1.0        # 복사 지연 (슬리피지 반영)
SLIPPAGE_PCT    = 0.05       # 슬리피지 0.05%


def fetch_leaderboard(limit=100):
    data = _proxy_get(f"leaderboard?limit={limit}")
    return data if isinstance(data, list) else data.get("data", [])


def analyze_traders(traders: list) -> list:
    """트레이더 분석 및 점수 계산"""
    results = []
    for t in traders:
        pnl_at  = float(t.get("pnl_all_time", 0) or 0)
        pnl_30d = float(t.get("pnl_30d", 0) or 0)
        pnl_7d  = float(t.get("pnl_7d", 0) or 0)
        pnl_1d  = float(t.get("pnl_1d", 0) or 0)
        vol_at  = max(float(t.get("volume_all_time", 1) or 1), 1)
        vol_30d = max(float(t.get("volume_30d", 1) or 1), 1)
        vol_7d  = max(float(t.get("volume_7d", 1) or 1), 1)
        equity  = float(t.get("equity_current", 0) or 0)
        oi      = float(t.get("oi_current", 0) or 0)

        # equity 기반 ROI (volume이 비정상 소수인 경우 방지)
        eq_base = max(equity, 1_000)  # 최소 $1000 기준
        roi_at  = pnl_at  / eq_base * 100
        roi_30d = pnl_30d / eq_base * 100
        roi_7d  = pnl_7d  / eq_base * 100

        consistency = sum([
            pnl_7d  > 0,
            pnl_30d > 0,
            pnl_at  > 0,
            pnl_1d  > 0,
        ])

        # Tier 분류
        if pnl_at > 0 and pnl_7d > 0 and pnl_30d >= MIN_TIER1_PNL30 and consistency == 4:
            tier = 1
        elif pnl_at > 0 and pnl_7d > 0 and pnl_30d >= MIN_TIER2_PNL30 and consistency >= 3:
            tier = 2
        elif pnl_at > 0 and pnl_7d > 0 and consistency >= 3:
            tier = 3
        else:
            tier = 0  # 팔로우 제외

        # OI 과도 레버리지 필터
        if oi > 500_000:
            tier = max(tier - 1, 0)

        # 종합 점수 (tier 보정)
        score = (roi_30d * 0.5 + roi_7d * 0.3 + roi_at * 0.2) * (consistency / 4)

        results.append({
            "address":    t["address"],
            "pnl_all_time": round(pnl_at, 0),
            "pnl_30d":    round(pnl_30d, 0),
            "pnl_7d":     round(pnl_7d, 0),
            "pnl_1d":     round(pnl_1d, 0),
            "roi_at":     round(roi_at, 4),
            "roi_30d":    round(roi_30d, 4),
            "roi_7d":     round(roi_7d, 4),
            "equity":     round(equity, 0),
            "oi":         round(oi, 0),
            "consistency": consistency,
            "tier":       tier,
            "score":      round(score, 4),
        })

    return sorted(results, key=lambda x: (x["tier"], x["score"]), reverse=True)


def backtest_portfolio(analyzed: list, capital: float = INITIAL_CAPITAL) -> dict:
    """
    백테스팅: 7일 PnL 기반 시뮬레이션
    - Tier 1/2 트레이더를 copy_ratio 비율로 팔로우
    - 슬리피지 + 수수료 반영
    """
    tier1 = [t for t in analyzed if t["tier"] == 1]
    tier2 = [t for t in analyzed if t["tier"] == 2]
    selected = (tier1 + tier2)[:10]  # 최대 10명

    if not selected:
        return {"error": "팔로우 대상 없음"}

    # 포트폴리오 배분 (동일 비중)
    alloc_per_trader = min(capital * MAX_PER_TRADER, capital / len(selected))
    taker_fee = 0.0004  # 0.04% taker fee

    results = []
    total_pnl_sim = 0.0
    total_fee = 0.0

    for t in selected:
        # 7일 ROI를 copy_ratio에 적용
        trader_roi_7d = t["roi_7d"] / 100  # 비율로 변환

        # 슬리피지 및 수수료 차감
        copy_pnl = alloc_per_trader * trader_roi_7d * COPY_RATIO
        slippage_cost = abs(copy_pnl) * (SLIPPAGE_PCT / 100)
        fee_cost = alloc_per_trader * taker_fee * 2  # 진입+청산

        net_pnl = copy_pnl - slippage_cost - fee_cost
        total_pnl_sim += net_pnl
        total_fee += fee_cost + slippage_cost

        results.append({
            "address":       t["address"],
            "tier":          t["tier"],
            "alloc_usd":     round(alloc_per_trader, 2),
            "trader_roi_7d": round(t["roi_7d"], 4),
            "copy_pnl":      round(copy_pnl, 2),
            "fee+slip":      round(fee_cost + slippage_cost, 2),
            "net_pnl":       round(net_pnl, 2),
        })

    final_capital = capital + total_pnl_sim
    roi_7d = (total_pnl_sim / capital) * 100

    return {
        "initial_capital":  capital,
        "final_capital":    round(final_capital, 2),
        "total_pnl_7d":    round(total_pnl_sim, 2),
        "roi_7d_pct":      round(roi_7d, 4),
        "total_fees":      round(total_fee, 2),
        "traders_followed": len(selected),
        "portfolio":        results,
    }


def print_report(analyzed: list, backtest: dict):
    """결과 리포트 출력"""
    tier_counts = {1: 0, 2: 0, 3: 0, 0: 0}
    for t in analyzed:
        tier_counts[t["tier"]] += 1

    print("=" * 60)
    print("  Copy Perp 백테스팅 리포트")
    print("=" * 60)
    print(f"  분석 트레이더: {len(analyzed)}명")
    print(f"  Tier 1 (즉시 팔로우): {tier_counts[1]}명")
    print(f"  Tier 2 (조건부):      {tier_counts[2]}명")
    print(f"  Tier 3 (관망):        {tier_counts[3]}명")
    print(f"  제외:                 {tier_counts[0]}명")
    print()

    print("── Tier 1 트레이더 ─────────────────────────────")
    tier1 = [t for t in analyzed if t["tier"] == 1]
    for i, t in enumerate(tier1[:5], 1):
        addr = t["address"][:8] + "..." + t["address"][-6:]
        print(f"  {i}. {addr}")
        print(f"     PnL: AT={t['pnl_all_time']:+,.0f} | 30d={t['pnl_30d']:+,.0f} | 7d={t['pnl_7d']:+,.0f}")
        print(f"     ROI: 30d={t['roi_30d']}% | 7d={t['roi_7d']}% | 일관성={t['consistency']}/4")
    print()

    print("── 백테스팅 결과 (7일, 초기자금 $10,000) ────────")
    if "error" not in backtest:
        print(f"  팔로우한 트레이더: {backtest['traders_followed']}명")
        print(f"  7일 순수익:       ${backtest['total_pnl_7d']:+,.2f}")
        print(f"  7일 ROI:          {backtest['roi_7d_pct']:+.4f}%")
        print(f"  최종 자산:        ${backtest['final_capital']:,.2f}")
        print(f"  총 수수료+슬리피지: ${backtest['total_fees']:,.2f}")
        print()
        print("  트레이더별 기여:")
        for p in backtest["portfolio"]:
            addr = p["address"][:8] + "..." + p["address"][-6:]
            print(f"    [{p['tier']}] {addr}  net={p['net_pnl']:+.2f}")
    print()
    print("=" * 60)


def main():
    print("Pacifica 리더보드 로딩 중...")
    traders = fetch_leaderboard(100)
    print(f"✅ {len(traders)}명 데이터 수집")

    analyzed = analyze_traders(traders)
    backtest = backtest_portfolio(analyzed)

    print_report(analyzed, backtest)

    # 결과 저장
    output = {
        "timestamp": int(time.time()),
        "traders":   analyzed,
        "backtest":  backtest,
        "follow_list": {
            "tier1": [t["address"] for t in analyzed if t["tier"] == 1],
            "tier2": [t["address"] for t in analyzed if t["tier"] == 2],
        }
    }
    with open("backtest_result.json", "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print("✅ backtest_result.json 저장 완료")


if __name__ == "__main__":
    main()
