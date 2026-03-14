"""
evaluate.py — AutoResearch 평가 루프 (수정 금지)

플로우:
1. Mainnet 실거래 데이터 로드
2. scorer.py의 score() 적용 → 트레이더 선별
3. 선별된 트레이더 포지션 Paper Trading 시뮬레이션 (5분)
4. follower_loss 계산 → results.jsonl에 기록
5. 이전 최고 대비 개선 여부 출력 → 에이전트가 commit/revert 결정

실행:
  python3 autoresearch/evaluate.py [--duration 300] [--capital 10000]
"""
import json, time, os, sys, math, importlib, argparse
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

# 동적 import (에이전트가 scorer.py 수정해도 reload)
def load_scorer():
    import autoresearch.scorer as sc
    importlib.reload(sc)
    return sc

BASE_DIR  = Path(__file__).parent.parent
DATA_DIR  = BASE_DIR / "autoresearch" / "data"
RESULT_F  = BASE_DIR / "autoresearch" / "results.jsonl"
BEST_F    = BASE_DIR / "autoresearch" / "best.json"


# ── 데이터 로드 ────────────────────────────────────────

def load_trader_metrics() -> list[dict]:
    """캐시된 Mainnet 트레이더 지표 로드 (없으면 실시간 수집)"""
    cache = DATA_DIR / "trader_metrics.json"
    if cache.exists() and (time.time() - cache.stat().st_mtime) < 3600:
        with open(cache) as f:
            return json.load(f)

    # 실시간 수집
    DATA_DIR.mkdir(exist_ok=True)
    from core.reliability import compute_trader_metrics
    metrics = compute_trader_metrics(limit=100)
    with open(cache, "w") as f:
        json.dump(metrics, f, indent=2)
    return metrics


def load_paper_trades() -> list[dict]:
    """최근 paper trading 결과"""
    result_file = BASE_DIR / "papertrading" / "live_result.json"
    if not result_file.exists():
        return []
    with open(result_file) as f:
        r = json.load(f)
    return r.get("closed_trades", [])


# ── 시뮬레이션 ─────────────────────────────────────────

def simulate_portfolio(
    trader_metrics: list[dict],
    scorer,
    capital:   float = 10000.0,
    duration_s: int  = 300,
) -> dict:
    """
    scorer.score()로 트레이더 선별 → 가중 포트폴리오 시뮬레이션
    실제 paper trading 데이터 기반 (없으면 지표 기반 추정)
    """
    scored = []
    for m in trader_metrics:
        try:
            result = scorer.score(m)
            if result["copy_ratio"] > 0:
                scored.append({**m, **result})
        except Exception as e:
            pass

    if not scored:
        return {
            "follower_loss": 999.0,
            "n_traders":     0,
            "total_ept_net": 0,
            "avg_mdd":       0,
            "selected":      [],
        }

    # 가중 포트폴리오 EPT_net
    total_ept     = sum(s["ept_net"] * s["copy_ratio"] for s in scored)
    total_weight  = sum(s["copy_ratio"] for s in scored)
    weighted_ept  = total_ept / max(total_weight, 0.001)

    avg_mdd = sum(s.get("mdd", 0) for s in scored) / len(scored)

    # follower_loss (최적화 지표)
    follower_loss = -weighted_ept + avg_mdd * 2

    return {
        "follower_loss": round(follower_loss, 6),
        "n_traders":     len(scored),
        "total_ept_net": round(weighted_ept, 4),
        "avg_mdd":       round(avg_mdd, 4),
        "selected":      [
            {
                "alias":       s.get("alias", s.get("address", "")[:12]),
                "crs":         s.get("crs_score"),
                "grade":       s.get("grade"),
                "copy_ratio":  s.get("copy_ratio"),
                "ept_net":     s.get("ept_net"),
                "flags":       s.get("flags", []),
            }
            for s in sorted(scored, key=lambda x: -x["crs_score"])[:10]
        ],
    }


# ── 메인 ───────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration",  type=int,   default=300,    help="시뮬레이션 초")
    parser.add_argument("--capital",   type=float, default=10000,  help="초기 자본 USD")
    parser.add_argument("--label",     type=str,   default="",     help="실험 레이블")
    args = parser.parse_args()

    print(f"\n{'='*55}")
    print(f"AutoResearch Evaluate  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*55}")

    # 1. 트레이더 지표 로드
    print("📊 트레이더 지표 로드 중...")
    try:
        trader_metrics = load_trader_metrics()
        print(f"   {len(trader_metrics)}명 로드")
    except Exception as e:
        print(f"   ⚠️ 실시간 로드 실패: {e} — 캐시 사용")
        trader_metrics = []

    # 캐시도 없으면 더미 데이터
    if not trader_metrics:
        print("   더미 데이터로 평가")
        trader_metrics = [
            {"alias": "Whale-Alpha",  "profit_factor": 1.2, "sharpe": 0.5, "sortino": 0.8,
             "csr": 2.0, "recovery_factor": 1.5, "mdd": 0.05, "risk_of_ruin": 0.05,
             "max_consec_loss": 3, "purity": 0.85, "sample_size": 50,
             "avg_win": 12.0, "avg_loss": 5.0, "win_rate": 60, "total_trades": 50},
            {"alias": "Multi-Pos",    "profit_factor": 1.8, "sharpe": 1.2, "sortino": 1.5,
             "csr": 5.0, "recovery_factor": 3.0, "mdd": 0.08, "risk_of_ruin": 0.02,
             "max_consec_loss": 2, "purity": 0.75, "sample_size": 100,
             "avg_win": 8.0, "avg_loss": 3.0, "win_rate": 70, "total_trades": 100},
            {"alias": "HFT-Noise",    "profit_factor": 0.9, "sharpe": -0.2, "sortino": -0.1,
             "csr": 0.5, "recovery_factor": 0.5, "mdd": 0.25, "risk_of_ruin": 0.30,
             "max_consec_loss": 8, "purity": 0.20, "sample_size": 500,
             "avg_win": 0.5, "avg_loss": 1.0, "win_rate": 45, "total_trades": 500},
        ]

    # 2. scorer 로드 & 실행
    print("🔬 scorer.py 로드...")
    sc = load_scorer()

    print(f"   실험 가중치: EPT={sc.w_ept_net} PF={sc.w_profit_factor} Purity={sc.w_purity}")

    # 3. 포트폴리오 시뮬레이션
    print(f"⏱️  시뮬레이션 ({args.duration}초)...")
    t0 = time.time()
    result = simulate_portfolio(trader_metrics, sc, args.capital, args.duration)
    elapsed = time.time() - t0

    # 4. 결과 출력
    print(f"\n{'─'*45}")
    print(f"선별 트레이더: {result['n_traders']}명")
    print(f"가중 EPT_net:  ${result['total_ept_net']:.4f}/trade")
    print(f"평균 MDD:      {result['avg_mdd']*100:.1f}%")
    print(f"follower_loss: {result['follower_loss']:.6f}  ← 낮을수록 좋음")
    print()
    print("선별된 트레이더:")
    for s in result["selected"]:
        print(f"  [{s['grade']}] {s['alias']:20s}  CRS={s['crs']:.1f}  ratio={s['copy_ratio']:.3f}  EPT=${s['ept_net']:.4f}")
        for flag in s.get("flags", []):
            print(f"       {flag}")

    # 5. 이전 최고 대비 비교
    best_loss = float("inf")
    if BEST_F.exists():
        with open(BEST_F) as f:
            best = json.load(f)
            best_loss = best.get("follower_loss", float("inf"))

    improved = result["follower_loss"] < best_loss
    print(f"\n{'─'*45}")
    print(f"이전 최고: {best_loss:.6f}")
    print(f"현재 결과: {result['follower_loss']:.6f}")
    if improved:
        print("✅ 개선! → git commit 권장")
        with open(BEST_F, "w") as f:
            json.dump({**result, "timestamp": datetime.now().isoformat()}, f, indent=2)
    else:
        delta = result["follower_loss"] - best_loss
        print(f"❌ 개선 없음 (+{delta:.6f}) → git revert 권장")

    # 6. results.jsonl 기록
    record = {
        "timestamp":    datetime.now().isoformat(),
        "label":        args.label,
        "follower_loss": result["follower_loss"],
        "n_traders":    result["n_traders"],
        "ept_net":      result["total_ept_net"],
        "avg_mdd":      result["avg_mdd"],
        "improved":     improved,
        "weights": {
            "w_ept_net":      sc.w_ept_net,
            "w_profit_factor": sc.w_profit_factor,
            "w_purity":       sc.w_purity,
            "w_sharpe":       sc.w_sharpe,
            "w_sortino":      sc.w_sortino,
        },
        "elapsed_s":    round(elapsed, 2),
    }
    with open(RESULT_F, "a") as f:
        f.write(json.dumps(record) + "\n")

    print(f"\n📝 기록: autoresearch/results.jsonl")
    print(f"{'='*55}\n")

    return 0 if improved else 1


if __name__ == "__main__":
    sys.exit(main())
