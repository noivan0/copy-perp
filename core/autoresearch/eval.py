"""
eval.py — AutoResearch 평가 루프
signal_config.py 파라미터 기반으로 CRS 계산 → 페이퍼트레이딩 5분 실행 → 결과 기록.

Usage:
    python3 core/autoresearch/eval.py [--tag <tag>] [--duration 300]
"""

import sys
import os
import json
import time
import csv
import subprocess
import importlib
import argparse
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "core"))

import signal_config as cfg

RESULTS_TSV = Path(__file__).parent / "results.tsv"
EVAL_DURATION = 300  # 5분


def load_mainnet_traders():
    """메인넷 상위 트레이더 데이터 로드 (캐시 우선)"""
    cache_path = Path(__file__).parent.parent.parent / "mainnet_traders.json"
    if cache_path.exists():
        with open(cache_path) as f:
            return json.load(f)
    return []


def compute_crs(trader: dict) -> tuple[float, str]:
    """
    signal_config.py 파라미터 기반 CRS 계산.
    Returns: (score, tier)
    """
    import math

    roi_30d = trader.get("roi_30d", 0) or 0
    pnl_7d  = trader.get("pnl_7d", 0) or 0
    pnl_30d = trader.get("pnl_30d", 1) or 1
    oi_eq   = trader.get("oi_eq_ratio", 1.0) or 1.0
    cons    = trader.get("consistency", 0) or 0
    pnl_1d  = trader.get("pnl_1d", 0) or 0

    # ── Hard Filter ──────────────────────────────────────────
    mom_ratio = pnl_7d / abs(pnl_30d) if pnl_30d != 0 else 0
    if mom_ratio < cfg.MIN_MOM_RATIO:
        return 0.0, "FILTERED"
    if oi_eq > cfg.MAX_OI_RATIO:
        return 0.0, "FILTERED"
    if cons < cfg.MIN_CONSISTENCY:
        return 0.0, "FILTERED"
    if roi_30d < cfg.MIN_ROI_30D:
        return 0.0, "FILTERED"
    if roi_30d > cfg.MAX_ROI_30D:
        return 0.0, "FILTERED"
    if pnl_7d < cfg.MIN_7D_PNL:
        return 0.0, "FILTERED"

    # ── Momentum 점수 (0~100) ─────────────────────────────────
    if cfg.MOM_SIGMOID_K > 0:
        # sigmoid: 1 / (1 + exp(-k*(x-x0)))
        x = mom_ratio
        k = cfg.MOM_SIGMOID_K
        x0 = cfg.MOM_SIGMOID_X0
        mom_score = 100 / (1 + math.exp(-k * (x - x0)))
    else:
        mom_score = min(100, max(0, mom_ratio * 200))

    # ── 수익성 점수 (0~100) ───────────────────────────────────
    if cfg.ROI_LOG_SCALE:
        roi_capped = min(roi_30d, cfg.ROI_SATURATION)
        profit_score = min(100, math.log1p(roi_capped) / math.log1p(cfg.ROI_SATURATION) * 100)
    else:
        profit_score = min(100, roi_30d / cfg.ROI_SATURATION * 100)

    # ── 리스크 점수 (0~100, OI/Equity 낮을수록 좋음) ─────────
    if cfg.OI_PENALTY_LINEAR:
        risk_score = max(0, 100 - (oi_eq - cfg.OI_SOFT_LIMIT) * 20)
    else:
        # 비선형: soft limit 이하면 100점, 초과 시 제곱 패널티
        excess = max(0, oi_eq - cfg.OI_SOFT_LIMIT)
        risk_score = max(0, 100 - excess ** 2 * 15)

    # ── 일관성 점수 (0~100) ───────────────────────────────────
    cons_score = min(100, (cons / 5.0) * 100)

    # ── 종합 CRS ─────────────────────────────────────────────
    crs = (
        cfg.W_MOMENTUM    * mom_score +
        cfg.W_PROFIT      * profit_score +
        cfg.W_RISK        * risk_score +
        cfg.W_CONSISTENCY * cons_score
    )

    # ── Tier ─────────────────────────────────────────────────
    if crs >= cfg.TIER_S_MIN_CRS:
        tier = "S"
    elif crs >= cfg.TIER_A_MIN_CRS:
        tier = "A"
    elif crs >= cfg.TIER_B_MIN_CRS:
        tier = "B"
    else:
        tier = "C"

    return round(crs, 2), tier


def rank_traders(traders: list) -> list:
    """CRS 기반 트레이더 랭킹"""
    scored = []
    for t in traders:
        score, tier = compute_crs(t)
        if tier != "FILTERED" and tier != "C":
            copy_ratio = {
                "S": cfg.TIER_S_COPY,
                "A": cfg.TIER_A_COPY,
                "B": cfg.TIER_B_COPY,
            }.get(tier, cfg.TIER_B_COPY)
            scored.append({**t, "crs": score, "tier": tier, "copy_ratio": copy_ratio})

    return sorted(scored, key=lambda x: x["crs"], reverse=True)


def run_mini_papertrading(selected_traders: list, duration_sec: int = 300) -> dict:
    """
    선별된 트레이더로 미니 페이퍼트레이딩 실행.
    mainnet trades/history API에서 실제 데이터 pull.
    """
    import ssl, socket

    MAINNET_IP = "54.230.62.105"
    INITIAL_CAPITAL = 10_000.0

    def mainnet_get(path, timeout=15):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        try:
            raw = socket.create_connection((MAINNET_IP, 443), timeout=timeout)
            s = ctx.wrap_socket(raw, server_hostname='api.pacifica.fi')
            req = (f'GET /api/v1/{path} HTTP/1.1\r\nHost: api.pacifica.fi\r\n'
                   f'Accept: application/json\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n')
            s.sendall(req.encode()); s.settimeout(timeout); data = b''
            while True:
                c = s.recv(32768)
                if not c: break
                data += c
            s.close()
            if b'\r\n\r\n' not in data: return []
            h, body = data.split(b'\r\n\r\n', 1)
            if b'chunked' in h.lower():
                dec = b''
                while body:
                    idx = body.find(b'\r\n')
                    if idx < 0: break
                    try: sz = int(body[:idx], 16)
                    except: break
                    if sz == 0: break
                    dec += body[idx+2:idx+2+sz]; body = body[idx+2+sz+2:]
                body = dec
            parsed = json.loads(body)
            return parsed.get('data', []) if isinstance(parsed, dict) else []
        except Exception as e:
            return []

    # 트레이더 거래 이력 수집
    all_trades = []
    for trader in selected_traders[:10]:  # 최대 10명
        addr = trader.get('address', '')
        if not addr:
            continue
        trades = mainnet_get(f'trades/history?account={addr}&limit=50')
        for t in trades:
            if isinstance(t, dict):
                all_trades.append({
                    'trader': addr,
                    'copy_ratio': trader.get('copy_ratio', 0.10),
                    'tier': trader.get('tier', 'B'),
                    **t
                })
        time.sleep(0.5)  # rate limit

    # 간단한 PnL 시뮬레이션
    capital = INITIAL_CAPITAL
    wins = losses = break_even = 0
    gross_profit = gross_loss = 0.0
    equity_series = [capital]
    peak_equity = capital

    for trade in all_trades:
        raw_pnl = float(trade.get('pnl', 0))
        copy_ratio = float(trade.get('copy_ratio', 0.10))
        copy_pnl = raw_pnl * copy_ratio

        capital += copy_pnl
        if copy_pnl > 0.001:
            wins += 1; gross_profit += copy_pnl
        elif copy_pnl < -0.001:
            losses += 1; gross_loss += abs(copy_pnl)
        else:
            break_even += 1

        peak_equity = max(peak_equity, capital)
        equity_series.append(capital)

    total_trades = wins + losses + break_even
    win_rate = wins / total_trades if total_trades > 0 else 0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (10.0 if gross_profit > 0 else 1.0)
    roi_pct = (capital - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    max_dd = (peak_equity - min(equity_series)) / peak_equity * 100 if equity_series else 0

    # Sharpe (simplified): roi / std of equity changes
    import statistics
    changes = [equity_series[i+1] - equity_series[i] for i in range(len(equity_series)-1)]
    std = statistics.stdev(changes) if len(changes) > 1 else 1.0
    sharpe = (capital - INITIAL_CAPITAL) / (std * (len(changes) ** 0.5)) if std > 0 else 0

    return {
        "roi_pct": round(roi_pct, 4),
        "win_rate": round(win_rate, 4),
        "profit_factor": round(min(profit_factor, 10.0), 4),
        "max_dd_pct": round(max_dd, 4),
        "sharpe_approx": round(sharpe, 4),
        "total_trades": total_trades,
        "n_traders": len(selected_traders),
        "gross_profit": round(gross_profit, 4),
        "gross_loss": round(gross_loss, 4),
    }


def log_result(tag: str, result: dict, description: str):
    """결과를 TSV에 기록"""
    import subprocess
    try:
        commit = subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD'],
                                        cwd=Path(__file__).parent.parent.parent).decode().strip()
    except Exception:
        commit = "unknown"

    header = ["commit", "tag", "sharpe", "roi_pct", "win_rate", "profit_factor",
              "max_dd", "n_traders", "description"]
    row = [
        commit, tag,
        result['sharpe_approx'], result['roi_pct'], result['win_rate'],
        result['profit_factor'], result['max_dd_pct'], result['n_traders'],
        description
    ]

    write_header = not RESULTS_TSV.exists()
    with open(RESULTS_TSV, 'a', newline='') as f:
        writer = csv.writer(f, delimiter='\t')
        if write_header:
            writer.writerow(header)
        writer.writerow(row)

    print(f"\n✅ 결과 기록: {RESULTS_TSV}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tag', default=cfg.EXPERIMENT_TAG)
    parser.add_argument('--duration', type=int, default=EVAL_DURATION)
    args = parser.parse_args()

    print("=" * 60)
    print(f"  CopyPerp AutoResearch — {args.tag}")
    print("=" * 60)
    print(f"\n파라미터:")
    print(f"  W_MOMENTUM={cfg.W_MOMENTUM}, W_PROFIT={cfg.W_PROFIT}")
    print(f"  W_RISK={cfg.W_RISK}, W_CONSISTENCY={cfg.W_CONSISTENCY}")
    print(f"  MIN_MOM_RATIO={cfg.MIN_MOM_RATIO}, MAX_OI_RATIO={cfg.MAX_OI_RATIO}")
    print(f"  ROI_LOG_SCALE={cfg.ROI_LOG_SCALE}, OI_PENALTY_LINEAR={cfg.OI_PENALTY_LINEAR}")

    # 트레이더 데이터 로드
    print("\n1. 트레이더 데이터 로드...")
    traders = load_mainnet_traders()
    print(f"   총 {len(traders)}명 로드")

    # CRS 계산 및 선별
    print("\n2. CRS 계산 및 필터링...")
    ranked = rank_traders(traders)
    print(f"   통과: {len(ranked)}명 | S:{sum(1 for t in ranked if t['tier']=='S')} "
          f"A:{sum(1 for t in ranked if t['tier']=='A')} B:{sum(1 for t in ranked if t['tier']=='B')}")

    if len(ranked) < 3:
        print("   ⚠️  트레이더 수 부족 — 필터 완화 필요")
        return

    print("\n   상위 5명:")
    for t in ranked[:5]:
        print(f"   [{t['tier']}] {t.get('alias','?'):12s} CRS={t['crs']:.1f} "
              f"ROI={t.get('roi_30d',0):.0%} copy={t['copy_ratio']:.0%}")

    # 페이퍼트레이딩 실행
    print(f"\n3. 페이퍼트레이딩 평가 (mainnet 실데이터)...")
    result = run_mini_papertrading(ranked, args.duration)

    # 결과 출력
    print(f"\n{'='*60}")
    print(f"  평가 결과")
    print(f"{'='*60}")
    print(f"  sharpe_approx:  {result['sharpe_approx']:>8.4f}")
    print(f"  roi_pct:        {result['roi_pct']:>+8.4f}%")
    print(f"  win_rate:       {result['win_rate']:>8.1%}")
    print(f"  profit_factor:  {result['profit_factor']:>8.2f}x")
    print(f"  max_dd_pct:     {result['max_dd_pct']:>8.4f}%")
    print(f"  n_traders:      {result['n_traders']:>8d}")
    print(f"  total_trades:   {result['total_trades']:>8d}")

    # TSV 기록
    log_result(args.tag, result, cfg.EXPERIMENT_NOTE)

    return result


if __name__ == "__main__":
    main()
