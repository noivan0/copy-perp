"""
트레이더 신뢰성 점수 계산 — Composite Reliability Score (CRS)
============================================================
Mainnet 실데이터 분석 기반 설계 (2026-03-14)

문제: 리더보드 PnL만으로는 팔로워 수익을 보장할 수 없음
- 펀딩비 수익자 (open만 하고 close 없음) → 팔로워 복사 불가
- 고빈도 소액 트레이더 → 슬리피지로 팔로워 수익 급감
- 단기 운 좋은 거래자 → 통계적 신뢰성 부족

CRS는 이런 함정을 걸러내는 복합 점수 (0~100)
"""
from __future__ import annotations
import math
from typing import Optional
from collections import Counter


# ── 등급 기준 ──────────────────────────────────────────
GRADE_THRESHOLDS = {
    "S":  80,   # Elite — 최대 15% 복사
    "A":  70,   # Recommended — 최대 10%
    "B":  60,   # Standard — 최대 7%
    "C":  50,   # Caution — 최대 5%
    "D":   0,   # Excluded — 팔로우 불가
}

MAX_COPY_RATIO = {
    "S": 0.15, "A": 0.10, "B": 0.07, "C": 0.05, "D": 0.0,
}

# ── 상수 ───────────────────────────────────────────────
TAKER_FEE    = 0.0006   # 0.06%
SLIPPAGE_EST = 0.0005   # 0.05% 슬리피지 추정
FOLLOWER_COST = TAKER_FEE + SLIPPAGE_EST  # 0.11% 총 비용


# ── 개별 지표 계산 함수 ────────────────────────────────

def compute_profit_factor(trades: list) -> float:
    """PF = |총수익| / |총손실| — 손실 없으면 999 반환"""
    pnls   = [float(t.get("pnl", 0) or 0) for t in trades]
    wins   = sum(p for p in pnls if p > 0)
    losses = abs(sum(p for p in pnls if p < 0))
    return wins / losses if losses > 0 else 999.0


def compute_win_rate(trades: list) -> float:
    """승률 (0.0~1.0) — pnl 있는 거래만"""
    pnls = [float(t.get("pnl", 0) or 0) for t in trades if float(t.get("pnl", 0) or 0) != 0]
    if not pnls:
        return 0.0
    return sum(1 for p in pnls if p > 0) / len(pnls)


def compute_expectancy(trades: list) -> float:
    """거래당 기대수익 (USDC) — 팔로워 복사 비용 차감 전"""
    pnls   = [float(t.get("pnl", 0) or 0) for t in trades if float(t.get("pnl", 0) or 0) != 0]
    if not pnls:
        return 0.0
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    wr = len(wins) / len(pnls)
    lr = len(losses) / len(pnls)
    avg_w = sum(wins) / len(wins) if wins else 0
    avg_l = sum(losses) / len(losses) if losses else 0
    return (wr * avg_w) + (lr * avg_l)


def compute_net_expectancy(trades: list, avg_position_usdc: float = 100.0) -> float:
    """팔로워 실제 기대수익 (슬리피지 + 수수료 차감 후)"""
    gross = compute_expectancy(trades)
    cost  = avg_position_usdc * FOLLOWER_COST
    return gross - cost


def compute_strategy_purity(trades: list) -> float:
    """
    전략 순도 (0.0~1.0)
    - 펀딩비 수익자 필터 핵심 지표
    - open 없이 보유만 하는 트레이더: 낮은 점수
    - 롱/숏 모두 활용하는 트레이더: 높은 점수
    """
    sides  = [t.get("side", "") for t in trades]
    opens  = sum(1 for s in sides if "open" in s)
    closes = sum(1 for s in sides if "close" in s)
    longs  = sum(1 for s in sides if "long" in s)
    shorts = sum(1 for s in sides if "short" in s)

    # close 비율: close가 많으면 실제 트레이딩 (진입+청산 모두)
    total = opens + closes
    close_ratio = closes / total if total > 0 else 0.0

    # 방향 다양성: 롱/숏 모두 사용
    total_dir = longs + shorts
    if total_dir == 0:
        direction_score = 0.0
    else:
        minority = min(longs, shorts)
        majority = max(longs, shorts)
        direction_score = minority / majority if majority > 0 else 0.0

    return close_ratio * 0.6 + direction_score * 0.4


def compute_consistency(pnl_1d: float, pnl_7d: float, pnl_30d: float) -> float:
    """
    일관성 점수 (0.0~1.0)
    단기/중기/장기 수익이 모두 양수이고 비율이 균일할수록 높음
    """
    if pnl_30d <= 0:
        return 0.0
    if pnl_7d <= 0:
        return 0.1  # 30일 양수지만 7일 음수: 낮은 일관성
    if pnl_1d <= 0:
        return 0.5  # 30d/7d 양수지만 1d 음수: 중간

    # 비율 일관성 (7d가 30d의 적절한 비율이면 좋음)
    ratio_7_30 = pnl_7d / pnl_30d  # 이상적: 0.1~0.5 (7일은 30일의 10~50%)
    if 0.05 <= ratio_7_30 <= 0.7:
        return 1.0
    elif ratio_7_30 > 0.7:
        return 0.8  # 7일이 너무 높으면 단기 편중 의심
    else:
        return 0.6


def compute_mdd(trades: list) -> float:
    """
    최대 낙폭 계산 (%)
    누적 PnL 시계열 기반
    """
    pnls = [float(t.get("pnl", 0) or 0) for t in sorted(trades, key=lambda x: x.get("created_at", 0))]
    cumulative = 0.0
    peak = 0.0
    mdd  = 0.0
    for pnl in pnls:
        cumulative += pnl
        peak = max(peak, cumulative)
        if peak > 0:
            dd = (peak - cumulative) / peak
            mdd = max(mdd, dd)
    return mdd * 100  # %


def compute_calmar(trades: list) -> float:
    """Calmar Ratio = 총 PnL / |최대 단일 손실|"""
    pnls    = [float(t.get("pnl", 0) or 0) for t in trades]
    total   = sum(pnls)
    max_loss = abs(min(pnls)) if pnls else 0
    return total / max_loss if max_loss > 0 else 999.0


def compute_concentration(trades: list) -> float:
    """심볼 집중도 (0~1) — 높을수록 특정 심볼에 집중"""
    symbols = Counter(t.get("symbol", "") for t in trades)
    total   = sum(symbols.values())
    if total == 0:
        return 1.0
    top1 = symbols.most_common(1)[0][1]
    return top1 / total


def estimate_avg_hold_time_hours(trades: list) -> float:
    """평균 포지션 보유 시간 추정 (시간)"""
    # open/close 쌍 매칭
    opens  = {t.get("symbol", ""): t.get("created_at", 0)
               for t in trades if "open" in t.get("side", "")}
    hold_times = []
    for t in trades:
        if "close" in t.get("side", ""):
            sym = t.get("symbol", "")
            if sym in opens and opens[sym]:
                hold_ms = t.get("created_at", 0) - opens[sym]
                if hold_ms > 0:
                    hold_times.append(hold_ms / 3600000)
    return sum(hold_times) / len(hold_times) if hold_times else 24.0


# ── 복합 점수 계산 ─────────────────────────────────────

def compute_crs(
    trades:     list,
    pnl_1d:     float = 0.0,
    pnl_7d:     float = 0.0,
    pnl_30d:    float = 0.0,
    equity:     float = 0.0,
) -> dict:
    """
    Composite Reliability Score (CRS) 계산
    
    Returns:
        dict with score (0~100), grade, all sub-metrics, flags
    """
    n = len(trades)

    # ── 부족한 데이터 처리 ──
    if n < 10:
        return {
            "crs": 0.0,
            "grade": "D",
            "max_copy_ratio": 0.0,
            "flags": ["데이터 부족 (<10건)"],
            "details": {},
        }

    # ── 개별 지표 ──
    pf          = compute_profit_factor(trades)
    wr          = compute_win_rate(trades)
    ept         = compute_expectancy(trades)
    purity      = compute_strategy_purity(trades)
    consistency = compute_consistency(pnl_1d, pnl_7d, pnl_30d)
    mdd         = compute_mdd(trades)
    calmar      = compute_calmar(trades)
    concentration = compute_concentration(trades)
    avg_hold_hrs  = estimate_avg_hold_time_hours(trades)

    # ── 가중 점수 계산 (합계 100점) ──
    score_pf          = (min(pf, 20) / 20) * 25          # 25점 (가장 중요)
    score_ept         = 15.0 if ept > 0 else 0.0         # 15점 (양수 필수)
    score_sample      = min(n / 100, 1.0) * 15           # 15점 (100건 = 만점)
    score_purity      = purity * 20                      # 20점 (펀딩비 필터)
    score_consistency = consistency * 15                 # 15점 (일관성)
    score_calmar      = (min(calmar, 50) / 50) * 10      # 10점

    raw = score_pf + score_ept + score_sample + score_purity + score_consistency + score_calmar

    # ── 페널티 ──
    flags = []

    # 펀딩비 수익자 의심: purity 낮고 PF 극단적
    if purity < 0.3 and pf > 15:
        raw *= 0.6
        flags.append("⚠️ 펀딩비 수익자 의심 (purity<0.3, PF극단)")

    # 데이터 부족 페널티
    if n < 30:
        raw *= 0.5
        flags.append(f"⚠️ 표본 부족 ({n}건, 30건 미만)")

    # 고손실 페널티
    if mdd > 30:
        raw *= 0.8
        flags.append(f"⚠️ MDD 높음 ({mdd:.1f}%)")

    # 집중도 위험
    if concentration > 0.8:
        flags.append(f"⚠️ 심볼 집중 ({concentration*100:.0f}% 단일 심볼)")

    # 포지션 보유 너무 길면 (48시간 이상) 팔로워 불리
    if avg_hold_hrs > 48:
        raw *= 0.9
        flags.append(f"⚠️ 장기 포지션 ({avg_hold_hrs:.0f}시간 평균 보유)")

    crs = min(max(raw, 0.0), 100.0)

    # ── 등급 결정 ──
    grade = "D"
    for g, threshold in sorted(GRADE_THRESHOLDS.items(), key=lambda x: x[1], reverse=True):
        if crs >= threshold:
            grade = g
            break

    # ── Hard filter: CRS 50 미만 강제 D ──
    if crs < 50:
        grade = "D"

    return {
        "crs":           round(crs, 2),
        "grade":         grade,
        "max_copy_ratio": MAX_COPY_RATIO[grade],
        "flags":         flags,
        "details": {
            "profit_factor":       round(pf, 2),
            "win_rate_pct":        round(wr * 100, 1),
            "expectancy_per_trade": round(ept, 3),
            "strategy_purity":     round(purity, 3),
            "consistency_score":   round(consistency, 3),
            "mdd_pct":             round(mdd, 2),
            "calmar_ratio":        round(calmar, 2),
            "concentration":       round(concentration, 3),
            "avg_hold_hrs":        round(avg_hold_hrs, 1),
            "sample_size":         n,
            # 가중 점수 분해
            "scores": {
                "profit_factor":  round(score_pf, 2),
                "ept_positive":   round(score_ept, 2),
                "sample_size":    round(score_sample, 2),
                "purity":         round(score_purity, 2),
                "consistency":    round(score_consistency, 2),
                "calmar":         round(score_calmar, 2),
            },
        },
    }


def summarize_crs(result: dict, alias: str = "") -> str:
    """CRS 결과 텍스트 요약"""
    d   = result["details"]
    crs = result["crs"]
    g   = result["grade"]
    lines = [
        f"{'='*50}",
        f"  {alias} — CRS: {crs:.1f} / 100  [{g}등급]",
        f"{'='*50}",
        f"  Profit Factor:    {d['profit_factor']:.2f}",
        f"  승률:             {d['win_rate_pct']:.1f}%",
        f"  Expectancy/trade: ${d['expectancy_per_trade']:+.3f}",
        f"  전략 순도:         {d['strategy_purity']:.2f}",
        f"  일관성:            {d['consistency_score']:.2f}",
        f"  MDD:              {d['mdd_pct']:.1f}%",
        f"  Calmar:           {d['calmar_ratio']:.2f}",
        f"  집중도:            {d['concentration']*100:.0f}%",
        f"  표본:              {d['sample_size']}건",
        f"  최대 복사비율:     {result['max_copy_ratio']*100:.0f}%",
    ]
    if result["flags"]:
        lines.append(f"  플래그: {' | '.join(result['flags'])}")
    return "\n".join(lines)


# ── 테스트 ─────────────────────────────────────────────
if __name__ == "__main__":
    import json, time, urllib.request, ssl, urllib.parse

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE

    def fetch(path, delay=3):
        time.sleep(delay)
        target = f"https://api.pacifica.fi/api/v1/{path}"
        proxy  = f"https://api.codetabs.com/v1/proxy?quest={target}"
        req    = urllib.request.Request(proxy, headers={"User-Agent": "CopyPerp/1.0"})
        with urllib.request.urlopen(req, context=ctx, timeout=20) as resp:
            r = json.loads(resp.read().decode("utf-8", "ignore"))
        return r.get("data") if isinstance(r, dict) and "data" in r else r

    # 리더보드에서 데이터 가져오기
    lb = fetch("leaderboard?limit=100", delay=3)
    lb_map = {t["address"]: t for t in lb} if lb else {}

    targets = [
        ("YjCD9Gek6MVY9t3MLEGYYdZLeaF6MZrpgZraayWsv9E", "Whale-Alpha"),
        ("GTU92nBC8LMyt9W4Qqc319BFR1vpkNNPAbt4QCnX7kZ6", "Multi-Strategy"),
        ("Ph9yECGodDAjiiSU9bpbJ8dds3ndWP1ngKo8h1K2QYv",  "Multi-Pos"),
    ]

    print("\n🔍 CRS 신뢰성 점수 계산\n")
    for addr, alias in targets:
        trades = fetch(f"trades/history?account={addr}&limit=100", delay=3)
        if not trades:
            print(f"{alias}: 거래 없음\n")
            continue

        lb_info = lb_map.get(addr, {})
        result  = compute_crs(
            trades   = trades,
            pnl_1d   = float(lb_info.get("pnl_1d",  0) or 0),
            pnl_7d   = float(lb_info.get("pnl_7d",  0) or 0),
            pnl_30d  = float(lb_info.get("pnl_30d", 0) or 0),
            equity   = float(lb_info.get("equity_current", 0) or 0),
        )
        print(summarize_crs(result, alias))
        print()
