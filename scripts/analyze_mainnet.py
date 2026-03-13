"""
Mainnet 트레이더 분석 스크립트
codetabs CORS 프록시로 api.pacifica.fi 접근

실행: python3 scripts/analyze_mainnet.py
결과: mainnet_trader_analysis.json, mainnet_backtest_result.json
"""

import json, time, urllib.request, urllib.parse, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MAINNET_URL = "https://api.pacifica.fi/api/v1"
CODETABS    = "https://api.codetabs.com/v1/proxy/?quest="


def proxy_get(path: str, retry: int = 3) -> dict:
    target = MAINNET_URL + "/" + path.lstrip("/")
    url    = CODETABS + urllib.parse.quote(target, safe="")
    for attempt in range(retry):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "CopyPerp/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read())
                return d.get("data", d) if isinstance(d, dict) and "data" in d else d
        except Exception as e:
            if attempt == retry - 1:
                raise
            time.sleep(2 ** attempt)


def score_trader(t: dict) -> float:
    p30 = float(t.get("pnl_30d", 0) or 0)
    p7  = float(t.get("pnl_7d",  0) or 0)
    pa  = float(t.get("pnl_all_time", 0) or 0)
    eq  = float(t.get("equity_current", 1) or 1)
    if eq <= 0:
        eq = 1
    roi30 = p30 / eq
    roi7  = p7  / eq
    consistency = sum([pa > 0, p30 > 0, p7 > 0])
    return roi30 * 0.6 + roi7 * 0.3 + (0.1 if consistency >= 3 else 0)


def classify(t: dict) -> str:
    p30 = float(t.get("pnl_30d", 0) or 0)
    p7  = float(t.get("pnl_7d",  0) or 0)
    pa  = float(t.get("pnl_all_time", 0) or 0)
    eq  = float(t.get("equity_current", 1) or 1)
    if eq <= 0:
        eq = 1
    if pa > 0 and p7 > 0 and p30 >= 50_000 and eq > 0:
        return "Tier1"
    if p30 >= 10_000 and eq > 0:
        return "Tier2"
    return "Excluded"


def main():
    print("🌐 Mainnet 트레이더 분석 시작 (api.pacifica.fi)")
    print("=" * 60)

    # 리더보드 수집
    print("📊 리더보드 100명 수집 중...")
    traders = proxy_get("leaderboard?limit=100")
    if isinstance(traders, dict):
        traders = list(traders.values())
    print(f"   수집 완료: {len(traders)}명")

    # 분류
    results = []
    tier1, tier2 = [], []
    for t in traders:
        tier = classify(t)
        sc   = score_trader(t)
        p30  = float(t.get("pnl_30d", 0) or 0)
        p7   = float(t.get("pnl_7d",  0) or 0)
        pa   = float(t.get("pnl_all_time", 0) or 0)
        eq   = float(t.get("equity_current", 1) or 1) or 1
        roi30 = round(p30 / eq * 100, 2)
        roi7  = round(p7  / eq * 100, 2)

        entry = {
            "address":     t.get("address"),
            "tier":        tier,
            "score":       round(sc, 4),
            "pnl_30d":     round(p30, 2),
            "pnl_7d":      round(p7,  2),
            "pnl_all_time": round(pa, 2),
            "equity":      round(eq,  2),
            "roi_30d_pct": roi30,
            "roi_7d_pct":  roi7,
            "oi_current":  float(t.get("oi_current", 0) or 0),
        }
        results.append(entry)
        if tier == "Tier1":
            tier1.append(entry)
        elif tier == "Tier2":
            tier2.append(entry)

    results.sort(key=lambda x: x["score"], reverse=True)
    tier1.sort(key=lambda x: x["pnl_30d"], reverse=True)
    tier2.sort(key=lambda x: x["pnl_30d"], reverse=True)

    # 저장
    out_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    analysis_path = os.path.join(out_dir, "mainnet_trader_analysis.json")
    with open(analysis_path, "w") as f:
        json.dump({"network": "mainnet", "fetched_at": int(time.time()),
                   "total": len(results), "tier1_count": len(tier1),
                   "tier2_count": len(tier2), "traders": results}, f, indent=2)

    # 백테스팅 (7일 시뮬레이션)
    initial = 10_000
    copy_ratio = 0.10
    pnl = 0.0
    top10 = tier1[:10] if len(tier1) >= 10 else tier1
    for t in top10:
        # 7일 ROI를 copy_ratio 적용
        roi7 = t["roi_7d_pct"] / 100
        allocation = initial * copy_ratio / len(top10)
        pnl += allocation * roi7
    final = initial + pnl
    roi7_total = pnl / initial * 100

    backtest = {
        "network": "mainnet",
        "initial_capital": initial,
        "copy_ratio": copy_ratio,
        "traders_used": len(top10),
        "tier1_total": len(tier1),
        "tier2_total": len(tier2),
        "7day_pnl": round(pnl, 2),
        "7day_roi_pct": round(roi7_total, 4),
        "final_equity": round(final, 2),
        "top_traders": [{"address": t["address"], "roi_30d": t["roi_30d_pct"],
                         "pnl_30d": t["pnl_30d"]} for t in tier1[:5]],
    }
    backtest_path = os.path.join(out_dir, "mainnet_backtest_result.json")
    with open(backtest_path, "w") as f:
        json.dump(backtest, f, indent=2)

    # 출력
    print(f"\n📈 분류 결과")
    print(f"   Tier1: {len(tier1)}명 | Tier2: {len(tier2)}명 | 제외: {len(results)-len(tier1)-len(tier2)}명")

    print(f"\n🏆 Mainnet Tier1 TOP 5")
    for i, t in enumerate(tier1[:5], 1):
        print(f"   {i}. {t['address'][:12]}... 30d=+${t['pnl_30d']:,.0f} ROI={t['roi_30d_pct']:.1f}%")

    print(f"\n📊 백테스팅 결과 (7일, copy_ratio={copy_ratio:.0%})")
    print(f"   초기자금: ${initial:,.0f}")
    print(f"   7일 PnL:  ${pnl:+,.2f}")
    print(f"   7일 ROI:  {roi7_total:+.4f}%")
    print(f"   최종자산: ${final:,.2f}")

    print(f"\n✅ 저장 완료")
    print(f"   {analysis_path}")
    print(f"   {backtest_path}")

    return backtest


if __name__ == "__main__":
    main()
