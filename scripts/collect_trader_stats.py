"""
트레이더 심층 통계 수집 스크립트
실제 Pacifica 테스트넷에서 각 트레이더의 거래내역 수집 → 승률/샤프/드로우다운 계산

실행: python3 scripts/collect_trader_stats.py
"""

import os, sys, json, time, math
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from pacifica.client import _proxy_get
import warnings
warnings.filterwarnings("ignore")


def fetch_trader_trades(address: str, limit: int = 100) -> list:
    """트레이더 거래 내역 조회"""
    try:
        data = _proxy_get(f"trades?account={address}&limit={limit}")
        if isinstance(data, list):
            return data
        return data.get("data", []) if isinstance(data, dict) else []
    except Exception:
        return []


def fetch_trader_positions(address: str) -> list:
    """트레이더 현재 포지션 조회"""
    try:
        data = _proxy_get(f"positions?account={address}")
        if isinstance(data, list):
            return data
        return data.get("data", []) if isinstance(data, dict) else []
    except Exception:
        return []


def compute_win_rate(trades: list) -> dict:
    """거래내역에서 승률 계산"""
    if not trades:
        return {"win_rate": None, "win_count": 0, "loss_count": 0, "total": 0}

    wins = 0
    losses = 0
    win_pnl = []
    loss_pnl = []

    for t in trades:
        pnl = float(t.get("realized_pnl", t.get("pnl", 0)) or 0)
        if pnl > 0:
            wins += 1
            win_pnl.append(pnl)
        elif pnl < 0:
            losses += 1
            loss_pnl.append(abs(pnl))

    total = wins + losses
    win_rate = wins / total * 100 if total > 0 else None

    avg_win = sum(win_pnl) / len(win_pnl) if win_pnl else 0
    avg_loss = sum(loss_pnl) / len(loss_pnl) if loss_pnl else 0
    profit_factor = avg_win / avg_loss if avg_loss > 0 else None

    return {
        "win_rate": round(win_rate, 1) if win_rate is not None else None,
        "win_count": wins,
        "loss_count": losses,
        "total_closed": total,
        "avg_win_usd": round(avg_win, 2),
        "avg_loss_usd": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2) if profit_factor else None,
    }


def compute_sharpe(pnl_7d: float, pnl_30d: float, equity: float) -> float:
    """샤프비율 근사치 계산 (일별 수익 표준편차 가정)"""
    if equity <= 0:
        return None
    # 30일 일별 평균 수익
    daily_pnl_avg = pnl_30d / 30
    # 7d와 30d 차이로 변동성 근사
    daily_7d = pnl_7d / 7
    volatility = abs(daily_7d - daily_pnl_avg)
    if volatility < 1:
        return None
    # 연환산 샤프비율 (리스크프리 0% 가정)
    sharpe = (daily_pnl_avg / volatility) * math.sqrt(252)
    return round(sharpe, 2)


def compute_max_drawdown(pnl_all_time: float, equity: float) -> float:
    """최대 드로우다운 근사치 (equity 기반)"""
    if equity <= 0:
        return None
    # 초기 자금 = equity - pnl_all_time
    initial = equity - pnl_all_time
    if initial <= 0:
        return None
    # 최고점 = equity (현재 포지션이 수익 중이면 더 높을 수 있으나 근사치)
    peak = max(equity, initial)
    drawdown = (peak - equity) / peak * 100
    return round(drawdown, 2)


def analyze_trading_style(equity: float, oi: float, vol_7d: float) -> str:
    """거래 스타일 분류"""
    if equity <= 0:
        return "unknown"
    leverage = oi / equity if equity > 0 else 0
    vol_ratio = abs(vol_7d) / equity if equity > 0 else 0

    if leverage > 3:
        style = "고레버리지"
    elif leverage > 1:
        style = "중레버리지"
    else:
        style = "저레버리지"

    if vol_ratio > 2:
        style += "+고빈도"
    elif vol_ratio > 0.5:
        style += "+중빈도"
    else:
        style += "+저빈도"

    return style


def main():
    print("리더보드 수집 중...")
    traders_raw = _proxy_get("leaderboard?limit=100")
    if isinstance(traders_raw, dict):
        traders_raw = traders_raw.get("data", [])

    # backtest_result.json에서 Tier 1/2 목록 로드
    tier_map = {}
    try:
        with open("backtest_result.json") as f:
            bt = json.load(f)
        for addr in bt.get("follow_list", {}).get("tier1", []):
            tier_map[addr] = 1
        for addr in bt.get("follow_list", {}).get("tier2", []):
            tier_map[addr] = 2
    except Exception:
        pass

    # Tier 1/2 트레이더만 심층 분석
    priority_traders = [t for t in traders_raw if t["address"] in tier_map]
    print(f"✅ 리더보드 {len(traders_raw)}명 중 Tier 1/2 대상: {len(priority_traders)}명")
    print()

    results = []
    for i, t in enumerate(priority_traders):
        addr = t["address"]
        tier = tier_map.get(addr, 0)
        short = addr[:8] + "..." + addr[-6:]

        pnl_at  = float(t.get("pnl_all_time", 0) or 0)
        pnl_30d = float(t.get("pnl_30d", 0) or 0)
        pnl_7d  = float(t.get("pnl_7d", 0) or 0)
        pnl_1d  = float(t.get("pnl_1d", 0) or 0)
        equity  = float(t.get("equity_current", 0) or 0)
        oi      = float(t.get("oi_current", 0) or 0)
        vol_7d  = float(t.get("volume_7d", 0) or 0)

        print(f"[{i+1}/{len(priority_traders)}] Tier {tier} {short} 분석 중...")

        # 거래 내역 수집 (rate limit 방지)
        trades = fetch_trader_trades(addr, limit=100)
        time.sleep(0.5)

        # 승률 계산
        win_stats = compute_win_rate(trades)

        # 샤프비율
        sharpe = compute_sharpe(pnl_7d, pnl_30d, equity)

        # 드로우다운
        dd = compute_max_drawdown(pnl_at, equity)

        # 스타일
        style = analyze_trading_style(equity, oi, vol_7d)

        # 복합 점수 (최종 랭킹용)
        eq_base = max(equity, 1000)
        roi_30d = pnl_30d / eq_base * 100
        roi_7d  = pnl_7d  / eq_base * 100
        consistency = sum([pnl_7d > 0, pnl_30d > 0, pnl_at > 0, pnl_1d > 0])

        win_bonus = (win_stats["win_rate"] or 50) / 100
        final_score = (roi_30d * 0.4 + roi_7d * 0.3 + (pnl_at / eq_base * 100) * 0.2) * win_bonus * (consistency / 4)

        result = {
            "address":      addr,
            "tier":         tier,
            "pnl_all_time": round(pnl_at, 0),
            "pnl_30d":      round(pnl_30d, 0),
            "pnl_7d":       round(pnl_7d, 0),
            "pnl_1d":       round(pnl_1d, 0),
            "equity":       round(equity, 0),
            "oi":           round(oi, 0),
            "roi_30d":      round(roi_30d, 4),
            "roi_7d":       round(roi_7d, 4),
            "consistency":  consistency,
            "trading_style": style,
            "win_rate":     win_stats["win_rate"],
            "win_count":    win_stats["win_count"],
            "loss_count":   win_stats["loss_count"],
            "total_trades": win_stats["total_closed"],
            "avg_win_usd":  win_stats["avg_win_usd"],
            "avg_loss_usd": win_stats["avg_loss_usd"],
            "profit_factor": win_stats["profit_factor"],
            "sharpe_approx": sharpe,
            "max_drawdown_pct": dd,
            "final_score":  round(final_score, 4),
        }
        results.append(result)

        print(f"    PnL 30d={pnl_30d:+,.0f} | 승률={win_stats['win_rate']}% | 샤프={sharpe} | DD={dd}% | 스타일={style}")

    # 최종 랭킹
    ranked = sorted(results, key=lambda x: x["final_score"], reverse=True)

    print()
    print("=" * 60)
    print("  최종 팔로우 추천 랭킹 (승률 반영)")
    print("=" * 60)
    for i, r in enumerate(ranked, 1):
        addr = r["address"][:8] + "..." + r["address"][-6:]
        print(f"{i:2}. [{r['tier']}] {addr}")
        print(f"    30d={r['pnl_30d']:+,.0f} ROI={r['roi_30d']}% | 승률={r['win_rate']}% | 샤프={r['sharpe_approx']} | DD={r['max_drawdown_pct']}%")
        print(f"    스타일={r['trading_style']} | 점수={r['final_score']}")

    # 저장
    output = {
        "timestamp": int(time.time()),
        "total_analyzed": len(results),
        "ranked_traders": ranked,
        "top10_addresses": [r["address"] for r in ranked[:10]],
    }
    with open("trader_deep_analysis.json", "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print()
    print("✅ trader_deep_analysis.json 저장 완료")


if __name__ == "__main__":
    main()
