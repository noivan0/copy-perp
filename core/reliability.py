"""
core/reliability.py
트레이더 성과 신뢰성 측정 — Composite Reliability Score (CRS)

핵심 철학: 단순 PnL이 아닌 "이 성과가 팔로워에게 재현 가능한가?"

3가지 신뢰성 함정을 필터링:
  1. 비활성 함정    — 최근엔 아무것도 안 함, 과거 성과로 포장
  2. 하락 전환 함정 — 30일 좋지만 최근 7일 급격히 하락 중
  3. 레버리지 함정  — OI/Equity 비율 극단, 팔로워 슬리피지 폭발

추가: trades/history 개별 거래 기반 통계 (Profit Factor, Expectancy, 연속손실)

입력: leaderboard 데이터 + (선택) trades/history 배열
출력: CRS 0~100, 티어 S/A/B/C/D, 권장 copy_ratio
"""
from __future__ import annotations
import math
from dataclasses import dataclass, field, asdict
from typing import Optional


# ────────────────────────────────────────────
# 등급 기준
# ────────────────────────────────────────────
GRADE = {"S": 80, "A": 65, "B": 50, "C": 35, "D": 0}
MAX_COPY_RATIO = {"S": 0.15, "A": 0.10, "B": 0.07, "C": 0.04, "D": 0.0}

# ────────────────────────────────────────────
# 하드 필터 (하나라도 해당 → D등급 즉시 제외)
# ────────────────────────────────────────────
HARD_FILTER = dict(
    min_equity_usdc   = 10_000,   # 최소 자산 $10k (너무 소규모 제외)
    min_pnl_30d       =  1_000,   # 30일 최소 수익 $1k
    max_oi_equity_ratio=   3.0,   # OI/Equity 최대 3배 (극단 레버리지 제외)
    min_consistency   =      2,   # 활동 일관성 최소 2 (1=거의 비활성)
    min_momentum_ratio=  -0.30,   # 7d/30d >= -30% (30일 대비 최근 -30% 초과 하락 제외)
    max_roi_30d       =   300.0,  # ROI 300% 초과 → 단발성 투기 의심
)


# ────────────────────────────────────────────
# 보조 함수
# ────────────────────────────────────────────
def _norm(v: float, lo: float, hi: float, invert: bool = False) -> float:
    """0~100 정규화, 항상 클램핑 보장"""
    if hi == lo:
        return 50.0
    s = (max(lo, min(hi, v)) - lo) / (hi - lo) * 100
    return round(max(0.0, min(100.0, 100 - s if invert else s)), 2)


def _safe(v, fallback=0.0):
    try:
        return float(v or 0)
    except Exception:
        return fallback


# ────────────────────────────────────────────
# 개별 거래 기반 계산 (trades/history 있을 때)
# ────────────────────────────────────────────
def calc_trade_stats(trades: list[dict]) -> dict:
    """
    trades/history 배열 → 세부 통계
    필드: pnl, amount, price, entry_price, cause, created_at, side
    """
    pnls = [_safe(t.get("pnl")) for t in trades
            if _safe(t.get("pnl")) != 0.0 and t.get("cause") != "liquidation"]

    if not pnls:
        return {}

    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    win_rate     = len(wins) / len(pnls)
    gross_win    = sum(wins)
    gross_loss   = abs(sum(losses))
    profit_factor= gross_win / gross_loss if gross_loss > 0 else 999.0
    avg_win      = gross_win / len(wins) if wins else 0
    avg_loss     = sum(losses) / len(losses) if losses else 0  # 음수
    expectancy   = win_rate * avg_win + (1 - win_rate) * avg_loss  # 거래당 기대값

    # 최대 연속 손실 계산
    max_streak = cur_streak = 0
    for p in pnls:
        if p < 0:
            cur_streak += 1
            max_streak = max(max_streak, cur_streak)
        else:
            cur_streak = 0

    # 청산 비율 (신뢰성 치명적)
    liq_count = sum(1 for t in trades if t.get("cause") == "liquidation")
    liq_rate  = liq_count / len(trades) if trades else 0

    # 평균 보유 시간 (분) — created_at ms 타임스탬프 기준
    hold_times = []
    by_symbol: dict[str, list] = {}
    for t in trades:
        sym  = t.get("symbol", "")
        side = t.get("side", "")
        ts   = _safe(t.get("created_at", 0)) / 1000  # ms → s
        by_symbol.setdefault(sym, []).append((ts, side, _safe(t.get("pnl"))))

    for sym, events in by_symbol.items():
        events.sort()
        for i in range(1, len(events)):
            dt = (events[i][0] - events[i-1][0]) / 60
            if 0 < dt < 10080:  # 1주일 이내만
                hold_times.append(dt)

    avg_hold_min = sum(hold_times) / len(hold_times) if hold_times else 60.0

    # 포지션 크기 분포 (슬리피지 영향 분석)
    position_usdc_list = []
    for t in trades:
        amt = _safe(t.get("amount"))
        ep  = _safe(t.get("entry_price")) or _safe(t.get("price"))
        if amt > 0 and ep > 0:
            position_usdc_list.append(amt * ep)
    avg_position_usdc = (sum(position_usdc_list) / len(position_usdc_list)
                         if position_usdc_list else 0)

    return dict(
        trade_count     = len(pnls),
        win_rate        = round(win_rate, 4),
        profit_factor   = round(min(profit_factor, 999), 4),
        avg_win         = round(avg_win, 4),
        avg_loss        = round(avg_loss, 4),
        expectancy      = round(expectancy, 4),
        max_consecutive_loss = max_streak,
        liquidation_rate= round(liq_rate, 4),
        avg_hold_min    = round(avg_hold_min, 2),
        avg_position_usdc = round(avg_position_usdc, 2),
        gross_profit    = round(gross_win, 2),
        gross_loss      = round(-gross_loss, 2),
    )


# ────────────────────────────────────────────
# 5차원 점수 계산
# ────────────────────────────────────────────

def _score_momentum(p30: float, p7: float, p1: float, roi30: float) -> tuple[float, list]:
    """
    모멘텀 신뢰성 (25%)
    핵심: 성과가 최근에도 유지되고 있는가?

    함정 탐지:
    - 비활성형: p7 ≈ 0 (최근에 아무것도 안 함)
    - 하락전환형: p7 << 0 (최근 급락)
    - 단발성: p7/p30 > 1.0 (한 주에 다 벌고 끝)
    """
    warnings = []
    if p30 <= 0:
        return 0.0, ["30일 수익 없음"]

    m7_30 = p7 / p30  # 7일이 30일에서 차지하는 비중

    # 이상적: 0.15 ~ 0.45 (7일이 30일의 15~45% 기여 → 꾸준히 버는 중)
    # 위험:   m7_30 < 0    (최근 손실)
    # 의심:   m7_30 ≈ 0    (비활성)
    # 단발:   m7_30 > 0.8  (한 주에 다 벌었음)

    if m7_30 < 0:
        ratio_score = _norm(m7_30, -0.5, 0, invert=True) * 0.4
        warnings.append(f"최근 7일 손실 (30일 대비 {m7_30:.0%})")
    elif m7_30 < 0.05:
        ratio_score = 20.0
        warnings.append(f"최근 비활성 (7일 기여 {m7_30:.1%})")
    elif m7_30 > 0.80:
        ratio_score = 55.0
        warnings.append(f"단발성 의심 (7일에 {m7_30:.0%} 집중)")
    else:
        # 이상적 범위 0.15~0.45에서 100점
        ratio_score = _norm(abs(m7_30 - 0.30), 0, 0.25, invert=True)

    # 1일 모멘텀 가점/감점 (최근 하루 방향)
    if p1 > 0:
        day_bonus = min(15.0, p1 / max(p30, 1) * 100)
    elif p1 < 0:
        day_bonus = max(-20.0, p1 / max(p30, 1) * 100)
        if p1 < -p30 * 0.05:
            warnings.append(f"오늘 손실 ${p1:,.0f}")
    else:
        day_bonus = 0

    score = max(0.0, min(100.0, ratio_score + day_bonus))
    return round(score, 1), warnings


def _score_profitability(p30: float, roi30: float, p7: float,
                         stats: dict) -> tuple[float, list]:
    """
    수익성 신뢰성 (30%)
    개별 거래 stats가 있으면 Profit Factor / Expectancy로 보강
    """
    warnings = []

    # ROI 점수: 5%~100% → 정상 범위, 100% 초과는 의심
    if roi30 > 200:
        roi_score = 40.0  # 고ROI는 오히려 감점 (단발 투기 의심)
        warnings.append(f"ROI {roi30:.0f}% — 단발 투기 의심")
    elif roi30 > 50:
        roi_score = 85.0
    elif roi30 >= 5:
        roi_score = _norm(roi30, 5, 50)
    elif roi30 >= 0:
        roi_score = _norm(roi30, 0, 5)
    else:
        roi_score = 0.0

    # Profit Factor (개별 거래 데이터 있을 때)
    pf = stats.get("profit_factor", 0)
    if pf > 0:
        pf_capped = min(pf, 5.0)
        pf_score = _norm(math.log(pf_capped + 0.01),
                         math.log(0.5), math.log(5.0))
        if pf > 100:
            warnings.append(f"Profit Factor {pf:.0f} — 이상치 의심")
        elif pf > 2.0:
            pass  # 강점
        elif pf < 1.2:
            warnings.append(f"Profit Factor {pf:.2f} 낮음")
    else:
        pf_score = 50.0  # 데이터 없으면 중립

    # Expectancy (거래당 기대값)
    exp = stats.get("expectancy", 0)
    if exp > 0:
        exp_score = min(100.0, _norm(exp, 0, 500))
    elif exp < 0:
        exp_score = 0.0
        warnings.append(f"거래당 기대값 ${exp:.2f} (음수)")
    else:
        exp_score = 50.0

    # 데이터 가중 합산
    if stats:
        score = roi_score * 0.35 + pf_score * 0.40 + exp_score * 0.25
    else:
        score = roi_score  # 개별 거래 없으면 ROI만
    return round(score, 1), warnings


def _score_risk(eq: float, oi: float, roi30: float, stats: dict) -> tuple[float, list]:
    """
    리스크 신뢰성 (25%)
    OI/Equity: 팔로워가 실제 따라갈 수 있는가 + 레버리지 과용
    """
    warnings = []

    # OI/Equity 비율 (레버리지 proxy)
    oi_ratio = oi / eq if eq > 0 else 0
    if oi_ratio > 3.0:
        oi_score = 0.0
        warnings.append(f"OI/Equity {oi_ratio:.1f}x — 극단적 레버리지")
    elif oi_ratio > 1.5:
        oi_score = _norm(oi_ratio, 1.5, 3.0, invert=True) * 0.5
        warnings.append(f"OI/Equity {oi_ratio:.1f}x (고위험)")
    else:
        oi_score = _norm(oi_ratio, 0, 1.5, invert=True)

    # 청산 비율 (있으면)
    liq_rate = stats.get("liquidation_rate", 0)
    if liq_rate > 0.05:
        liq_score = 0.0
        warnings.append(f"청산 비율 {liq_rate:.1%} (신뢰 불가)")
    elif liq_rate > 0.01:
        liq_score = 30.0
        warnings.append(f"청산 발생 {liq_rate:.1%}")
    else:
        liq_score = 100.0

    # 최대 연속 손실
    streak = stats.get("max_consecutive_loss", 0)
    streak_score = _norm(streak, 0, 10, invert=True)
    if streak > 5:
        warnings.append(f"최대 연속 손실 {streak}건")

    if stats:
        score = oi_score * 0.45 + liq_score * 0.30 + streak_score * 0.25
    else:
        score = oi_score
    return round(score, 1), warnings


def _score_consistency(cons: int, p30: float, p7: float, stats: dict) -> tuple[float, list]:
    """
    일관성 신뢰성 (15%)
    활동의 규칙성 — 꾸준히 매매하는가
    """
    warnings = []

    # API의 consistency 필드 (1~5 스케일 추정)
    cons_score = _norm(cons, 1, 5)
    if cons <= 1:
        warnings.append("활동 일관성 최하위 (비정기적 매매)")

    # 거래 빈도 (있으면)
    tc = stats.get("trade_count", 0)
    if tc > 0:
        if 10 <= tc <= 200:
            freq_score = 100.0
        elif tc < 5:
            freq_score = 20.0
            warnings.append(f"거래 {tc}건 — 표본 부족")
        else:
            freq_score = _norm(tc, 200, 500, invert=True)
    else:
        freq_score = 50.0

    # 평균 보유 시간 (복사 가능성)
    hold = stats.get("avg_hold_min", 60)
    if hold < 3:
        hold_score = 0.0
        warnings.append(f"평균 보유 {hold:.1f}분 — 복사 불가")
    elif hold < 10:
        hold_score = 40.0
        warnings.append(f"평균 보유 {hold:.1f}분 — 슬리피지 위험")
    else:
        hold_score = min(100.0, _norm(hold, 10, 480))

    if stats:
        score = cons_score * 0.30 + freq_score * 0.35 + hold_score * 0.35
    else:
        score = cons_score
    return round(score, 1), warnings


def _score_copyability(stats: dict, p30: float) -> tuple[float, list]:
    """
    복사 가능성 (5%)
    팔로워가 실제로 이 트레이더를 따라갈 수 있는가
    (포지션 크기, 보유시간, 슬리피지 추정)
    """
    warnings = []
    if not stats:
        return 60.0, []

    avg_pos = stats.get("avg_position_usdc", 0)
    hold    = stats.get("avg_hold_min", 60)

    # 포지션 크기: $50~$5000이 이상적
    if 50 <= avg_pos <= 5000:
        pos_score = 100.0
    elif avg_pos < 10:
        pos_score = 20.0
        warnings.append(f"평균 포지션 ${avg_pos:.0f} — 너무 소규모")
    elif avg_pos > 50000:
        pos_score = 30.0
        warnings.append(f"평균 포지션 ${avg_pos:,.0f} — 슬리피지 심각")
    else:
        pos_score = 70.0

    # 보유시간 기반 복사 가능성
    if hold < 3:
        copy_score = 0.0
    elif hold < 15:
        copy_score = 50.0
    else:
        copy_score = 100.0

    score = pos_score * 0.50 + copy_score * 0.50
    return round(score, 1), warnings


# ────────────────────────────────────────────
# 메인 CRS 계산
# ────────────────────────────────────────────

@dataclass
class CRSResult:
    address:      str
    alias:        str = ""

    # 종합
    crs:          float = 0.0   # Composite Reliability Score (0~100)
    grade:        str   = "D"
    disqualified: bool  = False
    disq_reason:  str   = ""
    recommended_copy_ratio: float = 0.0

    # 차원별
    momentum_score:     float = 0.0   # 25%
    profitability_score:float = 0.0   # 30%
    risk_score:         float = 0.0   # 25%
    consistency_score:  float = 0.0   # 15%
    copyability_score:  float = 0.0   # 5%

    # 강점 / 경고
    strengths: list = field(default_factory=list)
    warnings:  list = field(default_factory=list)

    # 원시 지표 (참고용)
    raw: dict = field(default_factory=dict)

    # 거래 통계 (있을 때)
    trade_stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        # raw는 별도 키로만
        return d

    def summary_line(self) -> str:
        stars = "⭐" * {"S":5,"A":4,"B":3,"C":2,"D":1}.get(self.grade, 1)
        flags = " ".join(["⚠️" + w[:20] for w in self.warnings[:2]])
        return (
            f"[{self.grade}] {stars} {self.alias or self.address[:10]:<12} "
            f"CRS={self.crs:.1f} | "
            f"mom={self.momentum_score:.0f} profit={self.profitability_score:.0f} "
            f"risk={self.risk_score:.0f} cons={self.consistency_score:.0f} | "
            f"copy={self.recommended_copy_ratio*100:.1f}% {flags}"
        )


def compute_crs(raw: dict, trades: list[dict] | None = None) -> CRSResult:
    """
    메인 CRS 계산 함수

    raw: leaderboard API 응답 한 줄 (address, pnl_30d, pnl_7d, pnl_1d, roi_30d,
                                      equity, oi, consistency, ...)
    trades: trades/history API 배열 (선택 — 있으면 정밀도 대폭 향상)
    """
    addr  = raw.get("address", "")
    alias = raw.get("alias", "") or raw.get("address", "")[:8]

    p30   = _safe(raw.get("pnl_30d"))
    p7    = _safe(raw.get("pnl_7d"))
    p1    = _safe(raw.get("pnl_1d"))
    p_at  = _safe(raw.get("pnl_all_time"))
    roi30 = _safe(raw.get("roi_30d"))
    eq    = _safe(raw.get("equity", 100000))
    oi    = _safe(raw.get("oi"))
    cons  = int(_safe(raw.get("consistency", 3)))

    result = CRSResult(address=addr, alias=alias)
    result.raw = {
        "pnl_30d": p30, "pnl_7d": p7, "pnl_1d": p1,
        "roi_30d": roi30, "equity": eq, "oi": oi,
        "consistency": cons,
    }

    # ── 하드 필터 ─────────────────────────────────
    oi_ratio    = oi / eq if eq > 0 else 0
    mom_ratio   = p7 / p30 if p30 > 0 else 0

    reasons = []
    if eq < HARD_FILTER["min_equity_usdc"]:
        reasons.append(f"자산 ${eq:,.0f} < ${HARD_FILTER['min_equity_usdc']:,}")
    if p30 < HARD_FILTER["min_pnl_30d"]:
        reasons.append(f"30일 PnL ${p30:,.0f} < ${HARD_FILTER['min_pnl_30d']:,}")
    if oi_ratio > HARD_FILTER["max_oi_equity_ratio"]:
        reasons.append(f"OI/Equity {oi_ratio:.1f}x > {HARD_FILTER['max_oi_equity_ratio']}x")
    if cons < HARD_FILTER["min_consistency"]:
        reasons.append(f"일관성 {cons} < {HARD_FILTER['min_consistency']}")
    if p30 > 0 and mom_ratio < HARD_FILTER["min_momentum_ratio"]:
        reasons.append(f"최근 하락 {mom_ratio:.0%} (30일 대비 -30% 초과)")
    if roi30 > HARD_FILTER["max_roi_30d"]:
        reasons.append(f"ROI {roi30:.0f}% 이상치 (단발 투기 의심)")

    if reasons:
        result.disqualified = True
        result.disq_reason  = " | ".join(reasons)
        result.grade        = "D"
        result.crs          = 0.0
        return result

    # ── 개별 거래 통계 ─────────────────────────────
    stats: dict = {}
    if trades:
        stats = calc_trade_stats(trades)
        result.trade_stats = stats

    # ── 5차원 점수 ─────────────────────────────────
    m_score, m_warn = _score_momentum(p30, p7, p1, roi30)
    p_score, p_warn = _score_profitability(p30, roi30, p7, stats)
    r_score, r_warn = _score_risk(eq, oi, roi30, stats)
    c_score, c_warn = _score_consistency(cons, p30, p7, stats)
    cp_score, cp_warn = _score_copyability(stats, p30)

    result.momentum_score      = m_score
    result.profitability_score = p_score
    result.risk_score          = r_score
    result.consistency_score   = c_score
    result.copyability_score   = cp_score

    # 가중 합산
    crs = (
        m_score  * 0.25 +
        p_score  * 0.30 +
        r_score  * 0.25 +
        c_score  * 0.15 +
        cp_score * 0.05
    )
    result.crs = round(crs, 1)

    # ── 등급 ─────────────────────────────────────
    for g, threshold in GRADE.items():
        if crs >= threshold:
            result.grade = g
            break
    result.recommended_copy_ratio = MAX_COPY_RATIO[result.grade]

    # ── 강점 / 경고 수집 ──────────────────────────
    all_warn = m_warn + p_warn + r_warn + c_warn + cp_warn

    strengths = []
    if roi30 > 30:
        strengths.append(f"ROI {roi30:.1f}%")
    if stats.get("profit_factor", 0) > 2.0:
        strengths.append(f"PF {stats['profit_factor']:.1f}")
    if stats.get("win_rate", 0) > 0.65:
        strengths.append(f"Win rate {stats['win_rate']:.0%}")
    if m_score > 70:
        strengths.append("Consistent momentum")
    if r_score > 80:
        strengths.append("Stable risk")
    if cons >= 4:
        strengths.append(f"Consistency {cons}/5")

    result.strengths = strengths
    result.warnings  = all_warn
    return result


def rank_by_crs(trader_list: list[dict],
                trades_map: dict[str, list] | None = None) -> tuple[list[CRSResult], list[CRSResult]]:
    """
    트레이더 목록 → CRS 계산 후 (통과, 제외) 분리 반환

    trades_map: {address: [trade, ...]} — trades/history 있을 때
    """
    passed, excluded = [], []
    for raw in trader_list:
        addr = raw.get("address", "")
        trades = (trades_map or {}).get(addr, [])
        r = compute_crs(raw, trades or None)
        (excluded if r.disqualified else passed).append(r)

    passed.sort(key=lambda x: x.crs, reverse=True)
    return passed, excluded


# ────────────────────────────────────────────
# 테스트 실행
# ────────────────────────────────────────────
if __name__ == "__main__":
    import json

    with open("backtest_result.json") as f:
        data = json.load(f)

    traders = data.get("traders", [])
    active  = [t for t in traders if _safe(t.get("pnl_30d")) > 0]
    print(f"전체: {len(traders)}명 | 30일 플러스: {len(active)}명\n")

    passed, excluded = rank_by_crs(active)

    print(f"{'='*75}")
    print(f" CRS 상위 트레이더 ({len(passed)}명 통과 / {len(excluded)}명 제외)")
    print(f"{'='*75}")
    for i, r in enumerate(passed[:12], 1):
        pnl30 = r.raw.get("pnl_30d", 0)
        roi30 = r.raw.get("roi_30d", 0)
        print(f"#{i:02d} {r.summary_line()}")
        if r.strengths:
            print(f"     ✅ {' | '.join(r.strengths)}")
        if r.warnings:
            print(f"     ⚠️  {' | '.join(r.warnings[:3])}")
        print()

    print(f"{'='*75}")
    print(f" 제외 트레이더 ({len(excluded)}명)")
    print(f"{'='*75}")
    for r in excluded[:10]:
        print(f"  ❌ [{r.alias}] {r.disq_reason}")

    # JSON 저장
    output = {
        "passed":   [r.to_dict() for r in passed],
        "excluded": [{"address": r.address, "alias": r.alias,
                      "reason": r.disq_reason} for r in excluded],
        "summary":  {
            "total": len(traders),
            "active": len(active),
            "passed": len(passed),
            "excluded_hard": len(excluded),
            "tier_dist": {g: sum(1 for r in passed if r.grade == g)
                          for g in ["S","A","B","C"]},
        }
    }
    with open("crs_result.json", "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\n✅ 결과 저장: crs_result.json")
    print(f"   티어 분포: {output['summary']['tier_dist']}")
