"""
트레이더 신뢰성 점수 v2 — Composite Reliability Score (CRS-v2)
==============================================================
학술/산업 표준 지표 전수 구현:
  - Sharpe, Sortino, Calmar (Sharpe 1966, Young 1991)
  - Ulcer Index / UPI (Peter Martin 1987)
  - Profit Factor, Expectancy, Kelly (Ralph Vince)
  - Risk of Ruin (Larry Williams)
  - Common Sense Ratio, Tail Ratio (Van Tharp)
  - GHPR (Geometric Holding Period Return)
  - Recovery Factor (ZuluTrade)
  - Strategy Purity (Copy Perp 자체 개발)

팔로워 수익 직결 원칙:
  EPT_net = EPT_gross - (avg_position × follower_cost)  > 0 필수
  follower_cost = taker_fee(0.06%) + slippage_est(0.05%) = 0.11%
"""
from __future__ import annotations
import math
from collections import Counter
from typing import Optional


# ── 비용 상수 ────────────────────────────────────────────
TAKER_FEE     = 0.0006
SLIPPAGE_EST  = 0.0005
FOLLOWER_COST = TAKER_FEE + SLIPPAGE_EST   # 0.11%

# ── 등급 체계 ─────────────────────────────────────────────
GRADES = {
    "S": {"min_crs": 80, "max_ratio": 0.15, "label": "Elite"},
    "A": {"min_crs": 70, "max_ratio": 0.10, "label": "Recommended"},
    "B": {"min_crs": 60, "max_ratio": 0.07, "label": "Standard"},
    "C": {"min_crs": 50, "max_ratio": 0.05, "label": "Caution"},
    "D": {"min_crs":  0, "max_ratio": 0.00, "label": "Excluded"},
}


# ═══════════════════════════════════════════════════════════
# 1. 원시 지표 계산 함수
# ═══════════════════════════════════════════════════════════

def compute_all_metrics(trades: list) -> dict:
    """
    거래 내역에서 전체 성과 지표를 계산한다.
    trades: [{'pnl': float, 'created_at': ms, 'side': str, 'fee': float, ...}]
    """
    pnl_series = [float(t.get("pnl", 0) or 0)
                  for t in sorted(trades, key=lambda x: x.get("created_at", 0))]
    pnl_hits   = [p for p in pnl_series if p != 0]
    wins       = [p for p in pnl_hits if p > 0]
    losses     = [p for p in pnl_hits if p < 0]
    fees       = [float(t.get("fee", 0) or 0) for t in trades]

    n = len(pnl_hits)
    if n == 0:
        return {"error": "no_pnl_data", "sample_n": 0}

    # ── 기본 통계 ─────────────────────────────────────────
    total_pnl  = sum(pnl_hits)
    total_fee  = sum(fees)
    win_rate   = len(wins) / n
    loss_rate  = 1.0 - win_rate
    avg_win    = sum(wins) / len(wins)     if wins   else 0.0
    avg_loss   = sum(losses) / len(losses) if losses else 0.0
    gross_wins = sum(wins)
    gross_loss = abs(sum(losses))

    # ── 수익성 ────────────────────────────────────────────

    # Profit Factor
    profit_factor = gross_wins / gross_loss if gross_loss > 0 else 999.0

    # Expectancy (Ralph Vince)
    expectancy = (win_rate * avg_win) + (loss_rate * avg_loss)

    # Payoff Ratio (Risk:Reward)
    payoff_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 999.0

    # Kelly Criterion (f*)
    kelly = 0.0
    if avg_loss != 0 and avg_win > 0:
        kelly = (win_rate * avg_win - loss_rate * abs(avg_loss)) / abs(avg_loss)
    kelly = max(0.0, kelly)

    # GHPR (Geometric Holding Period Return)
    ghpr = 0.0
    base = max(abs(avg_loss) * 10, 100.0) if avg_loss else 100.0
    if pnl_hits:
        product = 1.0
        for p in pnl_hits:
            product *= (1.0 + p / base)
        ghpr = (product ** (1.0 / n) - 1.0) * 100

    # ── 위험 ──────────────────────────────────────────────

    # MDD (수익률 기반 — 절대 PnL 기반 아님)
    cumulative = []
    cum = 0.0
    for p in pnl_series:
        cum += p
        cumulative.append(cum)

    peak, mdd = 0.0, 0.0
    for c in cumulative:
        peak = max(peak, c)
        if peak > 0:
            dd   = (peak - c) / peak
            mdd  = max(mdd, dd)
    mdd_pct = mdd * 100

    # MDD 절대값 (Recovery Factor용)
    mdd_abs = mdd * max(cumulative) if cumulative and max(cumulative) > 0 else 0.01

    # Recovery Factor (ZuluTrade 핵심 지표)
    recovery_factor = total_pnl / mdd_abs if mdd_abs > 0 else 999.0

    # Risk of Ruin (Larry Williams)
    risk_of_ruin = 0.0
    edge = win_rate - loss_rate
    if edge > 0 and loss_rate > 0:
        ror_base = (1.0 - edge) / (1.0 + edge)
        risk_of_ruin = ror_base ** (1.0 / max(abs(avg_loss), 1e-6))
    risk_of_ruin = min(max(risk_of_ruin, 0.0), 1.0) * 100  # %

    # Max Consecutive Losses
    max_consec_loss, cur = 0, 0
    for p in pnl_hits:
        if p < 0:
            cur += 1
            max_consec_loss = max(max_consec_loss, cur)
        else:
            cur = 0

    # ── 위험조정 수익 ──────────────────────────────────────

    # Sharpe Ratio (거래별)
    sharpe = 0.0
    if n > 1:
        mean_r = sum(pnl_hits) / n
        var    = sum((p - mean_r) ** 2 for p in pnl_hits) / (n - 1)
        std_r  = math.sqrt(var) if var > 0 else 0.0
        sharpe = (mean_r / std_r) * math.sqrt(n) if std_r > 0 else 0.0

    # Sortino Ratio (하방 변동성만 패널티)
    sortino = 0.0
    if losses and n > 1:
        mean_r       = sum(pnl_hits) / n
        down_var     = sum(l ** 2 for l in losses) / len(losses)
        downside_std = math.sqrt(down_var) if down_var > 0 else 0.0
        sortino      = (mean_r / downside_std) * math.sqrt(n) if downside_std > 0 else 999.0
    elif not losses:
        sortino = 999.0

    # Calmar Ratio
    calmar = total_pnl / mdd_abs if mdd_abs > 0 else 999.0

    # Ulcer Index (Peter Martin 1987)
    drawdowns = []
    peak2 = 0.0
    for c in cumulative:
        peak2 = max(peak2, c)
        dd2   = (peak2 - c) / peak2 if peak2 > 0 else 0.0
        drawdowns.append(dd2 * 100)
    ulcer_index = math.sqrt(sum(d ** 2 for d in drawdowns) / len(drawdowns)) if drawdowns else 0.0

    # UPI (Ulcer Performance Index)
    mean_r = sum(pnl_hits) / n
    upi    = mean_r / ulcer_index if ulcer_index > 0 else 0.0

    # Tail Ratio — 극단값 분포
    tail_ratio = 0.0
    if n >= 10:
        s   = sorted(pnl_hits)
        p5  = s[max(0, int(n * 0.05))]
        p95 = s[min(n - 1, int(n * 0.95))]
        tail_ratio = abs(p95 / p5) if p5 != 0 else 999.0

    # Common Sense Ratio (Van Tharp) = PF × Tail Ratio
    csr = profit_factor * tail_ratio if tail_ratio > 0 else 0.0

    # ── 활동성 / 전략 순도 ─────────────────────────────────
    tss = sorted([t.get("created_at", 0) for t in trades if t.get("created_at")])
    span_hrs = (max(tss) - min(tss)) / 3_600_000 if len(tss) > 1 else 0.0
    trades_per_day = len(trades) / (span_hrs / 24) if span_hrs > 0 else 0.0

    sides  = [t.get("side", "") for t in trades]
    opens  = sum(1 for s in sides if "open"  in s)
    closes = sum(1 for s in sides if "close" in s)
    longs  = sum(1 for s in sides if "long"  in s)
    shorts = sum(1 for s in sides if "short" in s)
    total_dir   = longs + shorts
    close_ratio = closes / (opens + closes) if (opens + closes) > 0 else 0.0
    dir_div     = min(longs, shorts) / max(longs, shorts) if max(longs, shorts) > 0 else 0.0
    purity      = close_ratio * 0.6 + dir_div * 0.4

    # 심볼 집중도
    syms        = Counter(t.get("symbol", "") for t in trades)
    top1_ratio  = syms.most_common(1)[0][1] / len(trades) if trades else 1.0

    return {
        # 기본
        "sample_n":        n,
        "total_pnl":       round(total_pnl, 4),
        "total_fee":       round(total_fee, 4),
        "net_pnl":         round(total_pnl - total_fee, 4),
        "win_rate":        round(win_rate, 4),        # 0~1
        "win_rate_pct":    round(win_rate * 100, 2),
        "avg_win":         round(avg_win, 4),
        "avg_loss":        round(avg_loss, 4),
        "payoff_ratio":    round(payoff_ratio, 3),
        # 수익성
        "profit_factor":   round(profit_factor, 3),
        "expectancy":      round(expectancy, 4),
        "kelly":           round(kelly, 4),           # 0~무한
        "kelly_pct":       round(kelly * 100, 2),
        "ghpr_pct":        round(ghpr, 4),
        # 위험
        "mdd_pct":         round(mdd_pct, 2),
        "recovery_factor": round(recovery_factor, 4),
        "risk_of_ruin":    round(risk_of_ruin, 4),   # %
        "max_consec_loss": max_consec_loss,
        # 위험조정 수익
        "sharpe":          round(sharpe, 4),
        "sortino":         round(sortino, 4),
        "calmar":          round(calmar, 4),
        "ulcer_index":     round(ulcer_index, 4),
        "upi":             round(upi, 4),
        "tail_ratio":      round(tail_ratio, 4),
        "common_sense_ratio": round(csr, 4),
        # 활동성
        "span_hrs":        round(span_hrs, 1),
        "trades_per_day":  round(trades_per_day, 2),
        # 전략 순도
        "strategy_purity": round(purity, 3),
        "close_ratio":     round(close_ratio, 3),
        "direction_div":   round(dir_div, 3),
        "top_symbol_ratio":round(top1_ratio, 3),
    }


# ═══════════════════════════════════════════════════════════
# 2. CRS-v2 복합 점수
# ═══════════════════════════════════════════════════════════

def compute_crs_v2(
    trades:   list,
    pnl_1d:   float = 0.0,
    pnl_7d:   float = 0.0,
    pnl_30d:  float = 0.0,
    equity:   float = 0.0,
) -> dict:
    """
    CRS-v2: Composite Reliability Score (0~100)
    팔로워 관점에서 트레이더 신뢰성을 평가한다.

    등급:
        S (80+) : 최대 15% 복사
        A (70+) : 최대 10% 복사
        B (60+) : 최대  7% 복사
        C (50+) : 최대  5% 복사
        D       : 팔로우 불가
    """
    m = compute_all_metrics(trades)
    if "error" in m:
        return _failed(m["error"])

    flags = []

    # ── Hard Filter (즉시 실격) ──────────────────────────
    if m["sample_n"] < 30:
        return _failed("데이터 부족 (<30건)")

    if m["strategy_purity"] < 0.25:
        return _failed("펀딩비/보유 전략 의심 (purity<0.25)")

    # 팔로워 실수익 EPT 계산
    avg_pos_size = (abs(m["avg_win"]) + abs(m["avg_loss"])) / 2.0
    follower_cost_usd = avg_pos_size * FOLLOWER_COST
    ept_net = m["expectancy"] - follower_cost_usd

    if ept_net <= 0:
        flags.append(f"⚠️ 팔로워 EPT 음수 (${ept_net:.4f}/trade)")

    # ── 점수 계산 (100점 만점) ───────────────────────────

    # A. 수익성 (35점)
    pf_norm      = min(m["profit_factor"] / 10.0, 1.0)   # PF 10 = 만점
    sharpe_norm  = min(m["sharpe"] / 3.0, 1.0)           # Sharpe 3 = 만점
    score_pf     = pf_norm * 15
    score_sharpe = sharpe_norm * 10
    score_ept    = 10.0 if ept_net > 0 else 0.0

    # B. 위험조정 수익 (30점)
    sortino_norm  = min(m["sortino"] / 10.0, 1.0)        # Sortino 10 = 만점
    csr_norm      = min(m["common_sense_ratio"] / 10.0, 1.0)  # CSR 10 = 만점
    rf_norm       = min(m["recovery_factor"] / 5.0, 1.0) # RF 5 = 만점
    score_sortino = sortino_norm * 15
    score_csr     = csr_norm * 10
    score_rf      = rf_norm * 5

    # C. 위험 (20점)
    ror_norm        = max(0.0, 1.0 - m["risk_of_ruin"] / 100)
    mdd_norm        = max(0.0, 1.0 - m["mdd_pct"] / 100)        # 수익률 기반 MDD
    consec_norm     = max(0.0, 1.0 - m["max_consec_loss"] / 20)
    score_ror       = ror_norm * 10
    score_mdd       = mdd_norm * 5
    score_consec    = consec_norm * 5

    # D. 전략 순도 / 활동성 (15점)
    purity_norm      = m["strategy_purity"]
    sample_norm      = min(m["sample_n"] / 100.0, 1.0)
    score_purity     = purity_norm * 10
    score_sample     = sample_norm * 5

    raw = (score_pf + score_sharpe + score_ept +
           score_sortino + score_csr + score_rf +
           score_ror + score_mdd + score_consec +
           score_purity + score_sample)

    # ── 페널티 ───────────────────────────────────────────
    if m["max_consec_loss"] > 15:
        raw *= 0.85
        flags.append(f"⚠️ 연속 손실 {m['max_consec_loss']}회")

    if m["risk_of_ruin"] > 20:
        raw *= 0.80
        flags.append(f"⚠️ 파산위험 {m['risk_of_ruin']:.1f}%")

    if m["top_symbol_ratio"] > 0.80:
        flags.append(f"⚠️ 단일 심볼 집중 {m['top_symbol_ratio']*100:.0f}%")

    # 일관성: 7d/30d PnL 비율
    consistency = _consistency(pnl_1d, pnl_7d, pnl_30d)
    if consistency < 0.5:
        raw *= 0.90
        flags.append("⚠️ 수익 일관성 낮음")

    crs = min(max(raw, 0.0), 100.0)

    # ── 등급 ─────────────────────────────────────────────
    grade = "D"
    for g, info in sorted(GRADES.items(), key=lambda x: x[1]["min_crs"], reverse=True):
        if crs >= info["min_crs"]:
            grade = g
            break

    max_ratio         = GRADES[grade]["max_ratio"]
    kelly_safe        = m["kelly"] * 0.25
    recommended_ratio = round(min(kelly_safe, max_ratio), 4)

    return {
        "crs":   round(crs, 1),
        "grade": grade,
        "grade_label":  GRADES[grade]["label"],
        "max_copy_ratio":         max_ratio,
        "recommended_copy_ratio": recommended_ratio,
        "ept_net":   round(ept_net, 4),
        "flags":     flags,
        "metrics":   m,
        "consistency": round(consistency, 3),
        "score_breakdown": {
            "profitability":  round(score_pf + score_sharpe + score_ept, 2),
            "risk_adjusted":  round(score_sortino + score_csr + score_rf, 2),
            "risk":           round(score_ror + score_mdd + score_consec, 2),
            "purity_activity":round(score_purity + score_sample, 2),
        },
    }


# ── 적응형 복사 비율 ──────────────────────────────────────

def adaptive_copy_ratio(crs_result: dict, current_drawdown_pct: float = 0.0) -> float:
    """
    현재 낙폭 상황을 반영한 동적 복사 비율
    낙폭이 클수록 베팅 축소 (Kelly 원칙)
    """
    base = crs_result.get("recommended_copy_ratio", 0.0)
    if current_drawdown_pct > 20:
        base *= 0.5
    elif current_drawdown_pct > 10:
        base *= 0.75
    return round(base, 4)


def slippage_breakeven(metrics: dict) -> float:
    """팔로워가 손익분기점이 되는 최대 슬리피지 (%)"""
    ept      = metrics.get("expectancy", 0.0)
    avg_pos  = (abs(metrics.get("avg_win", 0)) + abs(metrics.get("avg_loss", 0))) / 2.0
    return round(ept / avg_pos * 100, 4) if avg_pos > 0 else 0.0


# ── 내부 헬퍼 ─────────────────────────────────────────────

def _failed(reason: str) -> dict:
    return {
        "crs": 0.0, "grade": "D", "grade_label": "Excluded",
        "max_copy_ratio": 0.0, "recommended_copy_ratio": 0.0,
        "ept_net": 0.0, "flags": [f"❌ 실격: {reason}"],
        "metrics": {}, "consistency": 0.0,
        "score_breakdown": {"profitability": 0, "risk_adjusted": 0, "risk": 0, "purity_activity": 0},
    }


def _consistency(pnl_1d: float, pnl_7d: float, pnl_30d: float) -> float:
    """단기~장기 수익 방향 일관성 (0~1)"""
    if pnl_30d <= 0:
        return 0.0
    if pnl_7d <= 0:
        return 0.1
    if pnl_1d <= 0:
        return 0.5
    ratio = pnl_7d / pnl_30d
    return 1.0 if 0.05 <= ratio <= 0.7 else (0.8 if ratio > 0.7 else 0.6)


# ── 텍스트 요약 ───────────────────────────────────────────

def format_crs(result: dict, alias: str = "") -> str:
    m   = result.get("metrics", {})
    crs = result["crs"]
    g   = result["grade"]
    sb  = result["score_breakdown"]

    lines = [
        f"{'='*55}",
        f"  {alias}  CRS: {crs:.1f}/100  [{g} — {result['grade_label']}]",
        f"{'='*55}",
        f"  추천 복사비율: {result['recommended_copy_ratio']*100:.2f}%",
        f"  팔로워 EPT:   ${result['ept_net']:+.4f}/trade",
        f"",
        f"  [점수 분해]",
        f"    수익성 ({sb['profitability']:.1f}/35):  PF={m.get('profit_factor',0):.2f}  Sharpe={m.get('sharpe',0):.2f}  EPT양수={result['ept_net']>0}",
        f"    위험조정({sb['risk_adjusted']:.1f}/30): Sortino={m.get('sortino',0):.2f}  CSR={m.get('common_sense_ratio',0):.2f}  RF={m.get('recovery_factor',0):.2f}",
        f"    위험     ({sb['risk']:.1f}/20):  RoR={m.get('risk_of_ruin',0):.1f}%  MDD={m.get('mdd_pct',0):.1f}%  연속손실={m.get('max_consec_loss',0)}회",
        f"    순도     ({sb['purity_activity']:.1f}/15): Purity={m.get('strategy_purity',0):.2f}  표본={m.get('sample_n',0)}건",
    ]
    if result["flags"]:
        lines.append(f"  [플래그]")
        for f in result["flags"]:
            lines.append(f"    {f}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# 테스트 실행
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    import json, time, ssl, urllib.request

    _ctx = ssl.create_default_context()
    _ctx.check_hostname = False
    _ctx.verify_mode    = ssl.CERT_NONE

    def _fetch(path, delay=3):
        time.sleep(delay)
        target = f"https://api.pacifica.fi/api/v1/{path}"
        proxy  = f"https://api.codetabs.com/v1/proxy?quest={target}"
        req    = urllib.request.Request(proxy, headers={"User-Agent": "CopyPerp/1.0"})
        with urllib.request.urlopen(req, context=_ctx, timeout=20) as resp:
            r = json.loads(resp.read().decode("utf-8", "ignore"))
        return r.get("data") if isinstance(r, dict) and "data" in r else r

    lb = _fetch("leaderboard?limit=100", delay=3)
    lb_map = {t["address"]: t for t in lb} if lb else {}

    targets = [
        ("YjCD9Gek6MVY9t3MLEGYYdZLeaF6MZrpgZraayWsv9E", "Whale-Alpha"),
        ("GTU92nBC8LMyt9W4Qqc319BFR1vpkNNPAbt4QCnX7kZ6", "Multi-Strategy"),
        ("Ph9yECGodDAjiiSU9bpbJ8dds3ndWP1ngKo8h1K2QYv",  "Multi-Pos"),
    ]

    print("\n🔬 CRS-v2 신뢰성 점수 계산\n")
    for addr, alias in targets:
        trades = _fetch(f"trades/history?account={addr}&limit=100", delay=3)
        if not trades:
            print(f"{alias}: 거래 없음\n")
            continue
        lb_info = lb_map.get(addr, {})
        result  = compute_crs_v2(
            trades  = trades,
            pnl_1d  = float(lb_info.get("pnl_1d",  0) or 0),
            pnl_7d  = float(lb_info.get("pnl_7d",  0) or 0),
            pnl_30d = float(lb_info.get("pnl_30d", 0) or 0),
            equity  = float(lb_info.get("equity_current", 0) or 0),
        )
        print(format_crs(result, alias))
        sb = slippage_breakeven(result["metrics"])
        print(f"  손익분기 슬리피지: {sb:.4f}%\n")
