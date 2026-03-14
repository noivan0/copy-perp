"""
TraderScore v1 — 트레이더 성과 신뢰성 평가 모듈

7개 지표 기반 종합 스코어:
  - 다기간 일관성 (30%)
  - 최근 모멘텀 (15%)
  - ROI 기반 수익률 (20%)
  - 레버리지 리스크 (15%)
  - 자본 충분성 (10%)
  - 거래 활성도 (5%)
  - 올타임 양수 확인 (5%)
"""
from dataclasses import dataclass
from typing import Optional


WEIGHTS = {
    "consistency": 0.30,
    "momentum":    0.15,
    "roi":         0.20,
    "low_lev":     0.15,
    "cap":         0.10,
    "activity":    0.05,
    "alltime":     0.05,
}

# 필터 기준
MIN_SCORE         = 0.65      # 이 이하 트레이더 제외
MIN_EQUITY        = 50_000    # 최소 자본 $50k
MAX_LEVERAGE      = 15.0      # 최대 레버리지
MAX_TURNOVER      = 150.0     # 최대 회전율 (HFT 제외)
MIN_PNL_30D       = 5_000     # 30일 최소 수익 $5k


@dataclass
class TraderScore:
    address: str
    alias: str
    total_score: float

    # 세부 지표
    consistency: float    # 0~1: 다기간 일관성
    momentum: float       # 0~1: 최근 가속 여부
    roi_score: float      # 0~1: ROI 기반 점수
    lev_score: float      # 0~1: 레버리지 안전도
    cap_score: float      # 0~1: 자본 충분성
    activity_score: float # 0~1: 거래 활성도
    alltime_ok: float     # 0 or 1: 올타임 양수

    # 원본 지표
    pnl_1d: float
    pnl_7d: float
    pnl_30d: float
    pnl_all_time: float
    equity: float
    oi: float
    volume_30d: float
    roi_30d_pct: float
    leverage: float
    turnover_30d: float

    # 파생 지표
    weight: float = 0.0     # 포트폴리오 내 비중 (외부에서 설정)

    @property
    def is_eligible(self) -> bool:
        """팔로잉 자격 기준 통과 여부"""
        return (
            self.total_score >= MIN_SCORE
            and self.equity >= MIN_EQUITY
            and self.leverage <= MAX_LEVERAGE
            and self.turnover_30d <= MAX_TURNOVER
            and self.pnl_30d >= MIN_PNL_30D
        )

    @property
    def grade(self) -> str:
        """등급: A+ / A / B / C / D"""
        s = self.total_score
        if s >= 0.90: return "A+"
        if s >= 0.80: return "A"
        if s >= 0.70: return "B"
        if s >= 0.60: return "C"
        return "D"

    def summary(self) -> str:
        flags = []
        if self.pnl_7d < 0: flags.append("⚠️최근부진")
        if self.leverage > 10: flags.append("🔴고레버")
        if self.turnover_30d > 100: flags.append("⚡HFT")
        if self.pnl_1d < -5000: flags.append("❌오늘손실")
        flag_str = " ".join(flags) if flags else "✅안정"
        return (
            f"[{self.grade}] {self.alias[:12]:<12} "
            f"score={self.total_score:.3f} "
            f"pnl30=${self.pnl_30d:>+8,.0f} "
            f"ROI={self.roi_30d_pct:+.1f}% "
            f"lev={self.leverage:.1f}x "
            f"일관={self.consistency:.2f} {flag_str}"
        )


def score_trader(raw: dict) -> TraderScore:
    """
    리더보드 raw dict → TraderScore

    raw 예시:
      {'address': '...', 'username': '...', 'pnl_1d': '123.4',
       'pnl_7d': '456', 'pnl_30d': '789', 'pnl_all_time': '9999',
       'equity_current': '100000', 'oi_current': '50000', 'volume_30d': '1000000'}
    """
    def _f(key, default=0.0) -> float:
        v = raw.get(key)
        if v is None: return default
        try: return float(v)
        except (TypeError, ValueError): return default

    p1  = _f("pnl_1d")
    p7  = _f("pnl_7d")
    p30 = _f("pnl_30d")
    pat = _f("pnl_all_time")
    eq  = max(_f("equity_current"), 1.0)
    oi  = _f("oi_current")
    v30 = _f("volume_30d")

    # ── A) 다기간 일관성 ──────────────────────────────
    periods_pos = sum([p30 > 0, p7 > 0, p1 > 0])
    consistency = periods_pos / 3.0

    # ── B) 최근 모멘텀 ────────────────────────────────
    if p30 > 0:
        ratio_7d = p7 / abs(p30)
        expected = 7 / 30
        momentum = min(1.0, max(0.0, 0.5 + (ratio_7d - expected) * 2))
    elif p30 < 0:
        # 30일 손실: 7일도 손실이면 0, 7일 양수면 0.5 (회복 중)
        momentum = 0.5 if p7 > 0 else 0.0
    else:
        momentum = 0.5

    # ── C) ROI 기반 수익률 ────────────────────────────
    roi_30d = p30 / eq
    roi_score = min(1.0, max(0.0, roi_30d / 0.5))  # 50% ROI = 만점

    # ── D) 레버리지 리스크 ────────────────────────────
    lev = oi / eq
    lev_score = max(0.0, 1.0 - lev / 20.0)

    # ── E) 자본 충분성 ────────────────────────────────
    cap_score = min(1.0, max(0.0, (eq - 10_000) / 90_000))

    # ── F) 거래 활성도 ────────────────────────────────
    turnover = v30 / eq
    if 5 <= turnover <= 100:
        activity_score = 1.0
    elif turnover < 5:
        activity_score = turnover / 5
    else:
        activity_score = max(0.0, 1.0 - (turnover - 100) / 200)

    # ── G) 올타임 양수 ────────────────────────────────
    alltime_ok = 1.0 if pat > 0 else 0.3

    # ── 종합 ─────────────────────────────────────────
    total = (
        consistency    * WEIGHTS["consistency"] +
        momentum       * WEIGHTS["momentum"] +
        roi_score      * WEIGHTS["roi"] +
        lev_score      * WEIGHTS["low_lev"] +
        cap_score      * WEIGHTS["cap"] +
        activity_score * WEIGHTS["activity"] +
        alltime_ok     * WEIGHTS["alltime"]
    )

    alias = raw.get("username") or raw.get("address", "?")[:10]

    return TraderScore(
        address=raw.get("address", ""),
        alias=alias,
        total_score=round(total, 5),
        consistency=round(consistency, 4),
        momentum=round(momentum, 4),
        roi_score=round(roi_score, 4),
        lev_score=round(lev_score, 4),
        cap_score=round(cap_score, 4),
        activity_score=round(activity_score, 4),
        alltime_ok=round(alltime_ok, 4),
        pnl_1d=round(p1, 4),
        pnl_7d=round(p7, 4),
        pnl_30d=round(p30, 4),
        pnl_all_time=round(pat, 4),
        equity=round(eq, 4),
        oi=round(oi, 4),
        volume_30d=round(v30, 4),
        roi_30d_pct=round(roi_30d * 100, 2),
        leverage=round(lev, 2),
        turnover_30d=round(turnover, 1),
    )


def select_traders(leaderboard: list, top_n: int = 5) -> list[TraderScore]:
    """
    리더보드 list → 신뢰성 스코어 기반 상위 N명 선별

    Returns:
        포트폴리오 비중(weight)이 배분된 TraderScore 리스트
    """
    scored = [score_trader(t) for t in leaderboard]
    eligible = [s for s in scored if s.is_eligible]
    eligible.sort(key=lambda x: x.total_score, reverse=True)
    top = eligible[:top_n]

    if not top:
        # 기준 완화 후 재시도
        fallback = sorted(scored, key=lambda x: x.total_score, reverse=True)[:top_n]
        top = fallback

    # 가중치: score 비율로 배분
    total_score = sum(t.total_score for t in top) or 1
    for t in top:
        t.weight = round(t.total_score / total_score, 4)

    return top


def print_scorecard(traders: list[TraderScore], title: str = "트레이더 신뢰성 스코어"):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")
    for i, t in enumerate(traders):
        print(f"  [{i+1}] {t.summary()}")
        print(f"       weight={t.weight:.2f} | "
              f"일관={t.consistency:.2f} 모멘텀={t.momentum:.2f} "
              f"roi={t.roi_score:.2f} lev={t.lev_score:.2f} "
              f"cap={t.cap_score:.2f} activity={t.activity_score:.2f}")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    # 빠른 테스트: 실시간 리더보드 조회 후 선별
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from papertrading.run_papertrading import get_leaderboard

    print("리더보드 조회 중...")
    lb = get_leaderboard(100)
    traders = select_traders(lb, top_n=5)
    print_scorecard(traders, "실시간 Tier-A 트레이더 선별 결과")
