"""
Auto Research Loop for Copy Perp Signal Optimization
=====================================================
방법론: QuantaAlpha (arxiv 2602.07085) + Karpathy auto-research
- 가설 생성 → 지표 구현 → IC 검증 → 진화 루프

CARP Score 가중치 자동 최적화:
  W = [w_PF, w_Sharpe, w_Kelly, w_WR, w_MaxDD, w_Tail, w_PSR]
  각 가중치 조합 → 트레이더 점수 → copy 시뮬레이션 → PnL 측정
"""

import json, time, random, math, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from papertrading.paper_engine import _cf_get

TRADERS = [
    "4TYEjn9PSpxoBNBXufeuNDRbytzvyyZtEUgXYSk8kYLZ",
    "YjCD9Gek6MVY9t3MLEGYYdZLeaF6MZrpgZraayWsv9E",
    "HtC4WT6JhKz8eojNigfiqSWykG74kfQmyDeM9f753aPQ",
    "6ZjWoJKeD88JqREHhYAWSZVLQfVcMSbx6eVdajXt9Xbv",
    "E8j5xSbGXEWtj7BQobPtiMAfh7CpqR8t1tXX7qtAWCiZ",
]

def fetch_trader_trades(address, limit=300):
    r = _cf_get(f"trades/history?account={address}&limit={limit}")
    if not r: return []
    return r if isinstance(r, list) else r.get("data", []) if isinstance(r, dict) else []

def compute_features(trades):
    if not trades or len(trades) < 3:
        return None
    pnls = np.array([t.get("raw_pnl", 0) for t in trades if t.get("raw_pnl") is not None])
    if len(pnls) < 3:
        return None

    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    win_rate = len(wins) / len(pnls)
    avg_win = wins.mean() if len(wins) > 0 else 0
    avg_loss = abs(losses.mean()) if len(losses) > 0 else 1e-9
    gross_profit = wins.sum() if len(wins) > 0 else 0
    gross_loss = abs(losses.sum()) if len(losses) > 0 else 1e-9
    profit_factor = gross_profit / gross_loss

    mean_r = pnls.mean()
    std_r = pnls.std() if pnls.std() > 0 else 1e-9
    sharpe = mean_r / std_r
    neg = pnls[pnls < 0]
    downside = neg.std() if len(neg) > 0 and neg.std() > 0 else 1e-9
    sortino = mean_r / downside

    cumulative = np.cumsum(pnls)
    running_max = np.maximum.accumulate(cumulative)
    drawdown = running_max - cumulative
    max_dd = drawdown.max()
    max_dd_pct = max_dd / 1000.0
    ulcer = float(np.sqrt((drawdown**2).mean())) if max_dd > 0 else 0
    total_pnl = pnls.sum()
    recovery = total_pnl / max_dd if max_dd > 0 else 999.0

    n = len(pnls)
    skew = float(np.mean((pnls - mean_r)**3) / std_r**3) if std_r > 0 else 0
    kurt = float(np.mean((pnls - mean_r)**4) / std_r**4) - 3 if std_r > 0 else 0
    sr = sharpe
    inner = (n-1)**0.5 * (1 - skew * sr + (kurt - 1) / 4.0 * sr**2)
    if inner > 0:
        z = sr * inner**0.5
        psr = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    else:
        psr = 0.5

    rr = avg_win / avg_loss if avg_loss > 0 else 0
    kelly = win_rate - (1 - win_rate) / rr if rr > 0 else -1

    tail_ratio = 1.0
    if len(pnls) >= 10:
        p95 = np.percentile(pnls, 95)
        p05 = abs(np.percentile(pnls, 5))
        tail_ratio = float(p95 / p05) if p05 > 0 else 0

    notionals = [abs(t.get("amount", 0) * t.get("price", 0)) for t in trades]
    notionals = [v for v in notionals if v > 0]
    pos_cv = float(np.std(notionals) / np.mean(notionals)) if len(notionals) > 1 and np.mean(notionals) > 0 else 0

    max_consec_loss = cur = 0
    for p in pnls:
        if p < 0:
            cur += 1
            max_consec_loss = max(max_consec_loss, cur)
        else:
            cur = 0

    ic = 0.0
    if len(notionals) == len(pnls) and len(pnls) > 3 and np.std(notionals) > 0:
        ic = float(np.corrcoef(notionals, pnls)[0, 1])

    return {
        "n": n, "win_rate": win_rate,
        "profit_factor": min(profit_factor, 50.0),
        "sharpe": sharpe, "sortino": sortino, "kelly": kelly,
        "max_dd_pct": max_dd_pct, "ulcer": ulcer,
        "recovery": min(recovery, 50.0),
        "psr": psr, "tail_ratio": min(tail_ratio, 10.0),
        "pos_cv": pos_cv, "max_consec_loss": max_consec_loss,
        "ic": ic, "total_pnl": total_pnl, "mean_pnl": float(mean_r),
    }

def compute_carp_score(features, weights):
    if features is None:
        return -999
    f = features
    w = weights
    pf_score = min(f["profit_factor"] / 5.0, 1.0)
    sr_score = max(min((f["sharpe"] + 0.5) / 1.5, 1.0), 0)
    psr_score = f["psr"]
    kelly_score = max(min((f["kelly"] + 0.5) / 1.5, 1.0), 0)
    dd_penalty = max(0, 1.0 - f["max_dd_pct"] * 5)
    tail_score = min(f["tail_ratio"] / 3.0, 1.0)
    rec_score = min(f["recovery"] / 10.0, 1.0)
    return (
        w.get("profit_factor", 0.25) * pf_score +
        w.get("sharpe", 0.20) * sr_score +
        w.get("psr", 0.20) * psr_score +
        w.get("kelly", 0.10) * kelly_score +
        w.get("max_dd", 0.15) * dd_penalty +
        w.get("tail", 0.05) * tail_score +
        w.get("recovery", 0.05) * rec_score
    )

def backtest_with_weights(trader_features, weights, all_trades_by_trader):
    scores = {alias: compute_carp_score(feats, weights)
              for alias, feats in trader_features.items()}
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    selected = [(a, s) for a, s in ranked if s > 0.3][:3]
    if not selected:
        return {"roi": 0, "pf": 0, "max_dd": 0, "selected": [], "scores": scores}

    COPY_RATIO, MAX_POS = 0.05, 300
    all_copy_pnls, gross_p, gross_l = [], 0, 0
    for alias, _ in selected:
        for t in all_trades_by_trader.get(alias, []):
            raw = t.get("raw_pnl", 0)
            notional = abs(t.get("amount", 0) * t.get("price", 0))
            copy_notional = min(notional * COPY_RATIO, MAX_POS)
            cp = raw * (copy_notional / notional) if notional > 0 else 0
            all_copy_pnls.append(cp)
            if cp > 0: gross_p += cp
            else: gross_l += abs(cp)

    pf = gross_p / gross_l if gross_l > 0 else 999
    if all_copy_pnls:
        cs = np.cumsum(all_copy_pnls)
        max_dd = float((np.maximum.accumulate(cs) - cs).max())
    else:
        max_dd = 0

    return {
        "roi": sum(all_copy_pnls) / 10000.0 * 100,
        "pf": min(pf, 50),
        "max_dd": max_dd,
        "total_pnl": sum(all_copy_pnls),
        "selected": [a for a, _ in selected],
        "scores": {a: round(s, 3) for a, s in scores.items()},
    }

def mutate(weights, rate=0.15):
    new_w = weights.copy()
    for k in random.sample(list(new_w), random.randint(1, 2)):
        new_w[k] = max(0.01, new_w[k] + random.gauss(0, rate))
    total = sum(new_w.values())
    return {k: v/total for k, v in new_w.items()}

def crossover(w1, w2):
    result = {k: random.choice([w1[k], w2[k]]) for k in w1}
    total = sum(result.values())
    return {k: v/total for k, v in result.items()}

def fitness(result):
    roi = result.get("roi", 0)
    pf = result.get("pf", 0)
    max_dd = result.get("max_dd", 0) / 10000.0
    return roi + math.log(max(pf, 0.01)) * 0.5 - max_dd * 10

def auto_research_loop(trader_features, all_trades, generations=25, pop_size=15):
    print("=" * 60)
    print("🔬 AUTO RESEARCH LOOP (QuantaAlpha Evolutionary)")
    print(f"세대={generations}, 집단크기={pop_size}")
    print("=" * 60)

    weight_keys = ["profit_factor", "sharpe", "psr", "kelly", "max_dd", "tail", "recovery"]

    # Seed: 기존 CARP 기준값
    population = [{
        "profit_factor": 0.30, "sharpe": 0.25, "psr": 0.20,
        "kelly": 0.10, "max_dd": 0.10, "tail": 0.03, "recovery": 0.02,
    }]
    for _ in range(pop_size - 1):
        raw = {k: random.random() for k in weight_keys}
        total = sum(raw.values())
        population.append({k: v/total for k, v in raw.items()})

    best_ever, best_ever_fitness, history = None, -999, []

    for gen in range(generations):
        scored = []
        for w in population:
            result = backtest_with_weights(trader_features, w, all_trades)
            f_score = fitness(result)
            scored.append((w, result, f_score))
        scored.sort(key=lambda x: x[2], reverse=True)

        best_w, best_r, best_f = scored[0]
        if best_f > best_ever_fitness:
            best_ever_fitness = best_f
            best_ever = (best_w, best_r)

        history.append({
            "gen": gen+1, "best_fitness": round(best_f, 4),
            "best_roi": round(best_r.get("roi", 0), 4),
            "best_pf": round(best_r.get("pf", 0), 3),
            "max_dd": round(best_r.get("max_dd", 0), 2),
            "selected": best_r.get("selected", []),
            "weights": {k: round(v, 3) for k, v in best_w.items()},
        })

        print(f"[Gen {gen+1:>2}] fitness={best_f:.4f} | ROI={best_r.get('roi',0):+.4f}% | "
              f"PF={best_r.get('pf',0):.2f} | MaxDD=${best_r.get('max_dd',0):.2f} | "
              f"{best_r.get('selected', [])}")

        if gen < generations - 1:
            survivors = [w for w, _, _ in scored[:max(2, pop_size//2)]]
            new_pop = survivors[:2]
            while len(new_pop) < pop_size:
                if random.random() < 0.6 and len(survivors) >= 2:
                    child = crossover(*random.sample(survivors, 2))
                else:
                    child = mutate(random.choice(survivors))
                new_pop.append(child)
            population = new_pop

    return best_ever, history

def main():
    print("📡 Mainnet 트레이더 데이터 수집 중...\n")
    all_trades, trader_features = {}, {}

    for addr in TRADERS:
        alias = addr[:8]
        trades = fetch_trader_trades(addr, limit=300)
        all_trades[alias] = trades
        feats = compute_features(trades)
        trader_features[alias] = feats
        if feats:
            print(f"  {alias}: n={feats['n']}, WR={feats['win_rate']:.1%}, "
                  f"PF={feats['profit_factor']:.2f}, PSR={feats['psr']:.3f}, "
                  f"Sharpe={feats['sharpe']:.3f}, Kelly={feats['kelly']:.3f}")
        else:
            print(f"  {alias}: 데이터 부족")
        time.sleep(0.3)

    print()
    best, history = auto_research_loop(trader_features, all_trades, generations=25, pop_size=15)

    if best:
        bw, br = best
        print("\n" + "=" * 60)
        print("🏆 AUTO RESEARCH 최적 결과")
        print("=" * 60)
        print(f"ROI:           {br.get('roi', 0):+.4f}%")
        print(f"Profit Factor: {br.get('pf', 0):.3f}x")
        print(f"Max Drawdown:  ${br.get('max_dd', 0):.2f}")
        print(f"선택 트레이더: {br.get('selected', [])}")
        print("\n최적 CARP 가중치:")
        for k, v in sorted(bw.items(), key=lambda x: x[1], reverse=True):
            print(f"  {k:20s}: {v:.4f}")
        print("\n트레이더별 최적 CARP 점수:")
        for a, s in sorted(br.get("scores", {}).items(), key=lambda x: x[1], reverse=True):
            print(f"  {a}: {s:.4f}")

    os.makedirs("research", exist_ok=True)
    output = {
        "generated_at": time.time(),
        "method": "QuantaAlpha Evolutionary Auto Research Loop",
        "reference": "arxiv:2602.07085",
        "best_weights": best[0] if best else {},
        "best_result": best[1] if best else {},
        "history": history,
        "feature_summary": {
            alias: {k: round(v, 4) for k, v in feats.items()}
            for alias, feats in trader_features.items() if feats
        }
    }
    with open("research/auto_research_result.json", "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print("\n💾 결과 저장: research/auto_research_result.json")

if __name__ == "__main__":
    main()
