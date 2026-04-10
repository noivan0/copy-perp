"""
core/trader_scoring.py
트레이더 신뢰도 점수 시스템 (Trust Score)

리서치 기반:
- Apesteguia et al. 2020 (Management Science): 단일 지표 위험성
- Liu et al. 2023 (SSRN): 팔로워 이탈과 연속손실 상관관계
- QuantStats metrics: Sharpe, Sortino, Calmar, Profit Factor
- 실무: Bybit/Bitget elite trader criteria

5차원 평가: 수익성(30) + 위험관리(25) + 일관성(20) + 실행신뢰성(15) + 트랙레코드(10)
"""

import math
import json
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional

logger = logging.getLogger(__name__)

# ── 하드 필터 기준 ──────────────────────────────────────────────────
HARD_FILTER = {
    "min_win_rate":         30.0,   # 승률 최소 30%
    "min_profit_factor":     1.0,   # 손익비 최소 1.0 (수익 > 손실)
    "max_drawdown":         50.0,   # 최대 드로다운 50% 이하
    "min_trade_count_30d":   5,     # 30일 최소 5건
    "min_roi_30d":         -20.0,   # 30일 ROI -20% 이상
    "min_hold_time_min":     3.0,   # 평균 포지션 보유 최소 3분 (복사 가능성)
    "max_leverage":         20.0,   # 레버리지 20x 이하
}

# ── 티어 기준 ────────────────────────────────────────────────────────
TIER_THRESHOLDS = {
    "S": {"min_score": 80, "max_dd": 15, "min_win_rate": 65, "min_days": 90},
    "A": {"min_score": 65, "max_dd": 25, "min_win_rate": 55, "min_days": 30},
    "B": {"min_score": 50, "max_dd": 35, "min_win_rate": 40, "min_days": 14},
    "C": {"min_score":  0},
}


@dataclass
class TraderMetrics:
    """트레이더 원시 지표"""
    address: str
    alias: str = ""

    # 수익성
    roi_30d: float = 0.0          # 30일 ROI %
    roi_7d: float = 0.0           # 7일 ROI %
    pnl_30d: float = 0.0          # 30일 PnL (USDC)
    pnl_all_time: float = 0.0     # 전체 기간 PnL

    # 위험
    max_drawdown: float = 0.0     # 최대 드로다운 %
    current_equity: float = 0.0   # 현재 자산 (USDC)
    avg_leverage: float = 1.0     # 평균 레버리지
    max_leverage: float = 1.0     # 최대 사용 레버리지

    # 일관성
    win_rate: float = 0.0         # 승률 %
    trade_count: int = 0          # 전체 거래 수
    trade_count_30d: int = 0      # 30일 거래 수
    profit_factor: float = 0.0    # Σ수익 / Σ|손실|
    avg_win: float = 0.0          # 평균 수익 거래 (USDC)
    avg_loss: float = 0.0         # 평균 손실 거래 (USDC, 음수)
    max_consecutive_loss: int = 0 # 최대 연속 손실

    # 실행
    avg_hold_time_min: float = 60.0  # 평균 포지션 보유 시간 (분)
    avg_position_usdc: float = 100.0 # 평균 포지션 크기 (USDC)
    open_positions: int = 0          # 현재 오픈 포지션 수

    # 트랙레코드
    trading_days: int = 0            # 활동 일수
    monthly_positive_rate: float = 0.0  # 플러스 월 비율 %


@dataclass
class TraderScore:
    """트레이더 신뢰도 점수"""
    address: str
    alias: str

    # 차원별 점수 (0~100)
    profitability_score: float = 0.0
    risk_score: float = 0.0
    consistency_score: float = 0.0
    execution_score: float = 0.0
    track_record_score: float = 0.0

    # 종합
    trust_score: float = 0.0
    tier: str = "C"
    disqualified: bool = False
    disqualify_reason: str = ""

    # 추천 복사 비율
    recommended_copy_ratio: float = 0.0

    # 상세 설명
    strengths: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _normalize(value: float, min_v: float, max_v: float,
               invert: bool = False) -> float:
    """값을 0~100으로 정규화. invert=True면 낮을수록 좋음"""
    if max_v == min_v:
        return 50.0
    clamped = max(min_v, min(max_v, value))
    score = (clamped - min_v) / (max_v - min_v) * 100
    score = max(0.0, min(100.0, score))  # 항상 0~100 보장
    return 100 - score if invert else score


def _safe_log(x: float, fallback: float = 0.0) -> float:
    try:
        return math.log(x) if x > 0 else fallback
    except Exception:
        return fallback


def score_profitability(m: TraderMetrics) -> tuple[float, list, list]:
    """수익성 점수 (0~100)"""
    strengths, warnings = [], []

    # ROI 30d: -20% ~ +50% → 0~100
    roi_score = _normalize(m.roi_30d, -20, 50)

    # Profit Factor: 0.5 ~ 5.0 → 0~100 (log 스케일)
    # 100 초과 이상치는 데이터 오류 가능성 → 경고 후 클램핑
    pf_raw = m.profit_factor
    pf_capped = min(pf_raw, 5.0)  # 5.0 이상은 클램핑 (이상치 방지)
    pf_score = _normalize(_safe_log(pf_capped, 0), _safe_log(0.5), _safe_log(5))

    # Win/Loss Ratio
    avg_loss_abs = abs(m.avg_loss) if m.avg_loss < 0 else max(m.avg_loss, 1)
    wl_ratio = m.avg_win / avg_loss_abs if avg_loss_abs > 0 else 1.0
    wl_score = _normalize(wl_ratio, 0.5, 3.0)

    score = roi_score * 0.40 + pf_score * 0.40 + wl_score * 0.20

    if m.roi_30d > 20:
        strengths.append(f"30d ROI {m.roi_30d:.1f}% — strong")
    if m.profit_factor > 100:
        warnings.append(f"Profit Factor {m.profit_factor:.0f} — 이상치 의심 (데이터 검증 필요)")
    elif m.profit_factor > 2.0:
        strengths.append(f"Profit Factor {m.profit_factor:.1f} — excellent risk/reward")
    if m.roi_30d < 0:
        warnings.append(f"30일 수익률 마이너스 ({m.roi_30d:.1f}%)")
    if m.profit_factor < 1.2:
        warnings.append("손익비 낮음 (1.2 미만)")

    return round(score, 1), strengths, warnings


def score_risk(m: TraderMetrics) -> tuple[float, list, list]:
    """위험관리 점수 (0~100)"""
    strengths, warnings = [], []

    # Max Drawdown: 낮을수록 좋음 (0% ~ 50%)
    dd_score = _normalize(m.max_drawdown, 0, 50, invert=True)

    # Sharpe 근사 (데이터 부족시): roi_30d / volatility_proxy
    # 여기선 profit_factor와 drawdown으로 근사
    # Calmar = roi_annualized / max_drawdown
    roi_annualized = m.roi_30d * 12  # 월간 ROI를 연간으로 근사
    calmar = roi_annualized / max(m.max_drawdown, 1.0)
    calmar_score = _normalize(calmar, -5, 10)

    # 레버리지: 낮을수록 좋음 (1x ~ 20x)
    lev_score = _normalize(m.avg_leverage, 1, 20, invert=True)

    score = dd_score * 0.45 + calmar_score * 0.35 + lev_score * 0.20

    if m.max_drawdown < 15:
        strengths.append(f"Max DD {m.max_drawdown:.1f}% — stable")
    elif m.max_drawdown > 35:
        warnings.append(f"최대 드로다운 {m.max_drawdown:.1f}% (위험)")
    if m.avg_leverage > 10:
        warnings.append(f"평균 레버리지 {m.avg_leverage:.1f}x (고위험)")
    if m.max_leverage > 15:
        warnings.append(f"최대 레버리지 {m.max_leverage:.0f}x 사용")

    return round(score, 1), strengths, warnings


def score_consistency(m: TraderMetrics) -> tuple[float, list, list]:
    """일관성 점수 (0~100)"""
    strengths, warnings = [], []

    # 승률: 30% ~ 80%
    wr_score = _normalize(m.win_rate, 30, 80)

    # 월별 플러스 비율: 0% ~ 100%
    monthly_score = _normalize(m.monthly_positive_rate, 0, 100)

    # 연속 손실: 낮을수록 좋음 (0~10)
    streak_score = _normalize(m.max_consecutive_loss, 0, 10, invert=True)

    # 거래 활성도: 5건 ~ 200건/30일 (중간이 최적)
    tc = m.trade_count_30d
    if 20 <= tc <= 150:
        activity_score = 100
    elif tc < 5:
        activity_score = 0
    elif tc < 20:
        activity_score = _normalize(tc, 5, 20)
    else:
        activity_score = _normalize(tc, 150, 300, invert=True)

    score = wr_score * 0.35 + monthly_score * 0.30 + streak_score * 0.20 + activity_score * 0.15

    if m.win_rate > 65:
        strengths.append(f"Win rate {m.win_rate:.1f}% — strong")
    if m.max_consecutive_loss > 5:
        warnings.append(f"최대 연속 손실 {m.max_consecutive_loss}건")
    if m.trade_count_30d < 5:
        warnings.append(f"30일 거래 {m.trade_count_30d}건 (비활성)")
    if m.trade_count_30d > 300:
        warnings.append(f"30일 거래 {m.trade_count_30d}건 (초단타 의심)")

    return round(score, 1), strengths, warnings


def score_execution(m: TraderMetrics) -> tuple[float, list, list]:
    """실행 신뢰성 점수 (0~100)"""
    strengths, warnings = [], []

    # 평균 포지션 보유 시간: 3분 ~ 1440분(1일), 짧을수록 복사 어려움
    hold_score = _normalize(min(m.avg_hold_time_min, 1440), 3, 1440)

    # 포지션 크기: 팔로워 복사 용이성 (너무 크면 슬리피지)
    # $10 ~ $10,000 기준, $100~$1000이 최적
    pos = m.avg_position_usdc
    if 50 <= pos <= 2000:
        pos_score = 100
    elif pos < 10:
        pos_score = 20
    elif pos < 50:
        pos_score = _normalize(pos, 10, 50)
    else:
        pos_score = _normalize(pos, 2000, 50000, invert=True)

    score = hold_score * 0.60 + pos_score * 0.40

    if m.avg_hold_time_min < 5:
        warnings.append(f"평균 보유 {m.avg_hold_time_min:.1f}분 (복사 불가 위험)")
    elif m.avg_hold_time_min >= 60:
        strengths.append(f"Avg hold {m.avg_hold_time_min:.0f}min — easy to copy")
    if m.avg_position_usdc > 5000:
        warnings.append(f"평균 포지션 ${m.avg_position_usdc:.0f} (슬리피지 위험)")

    return round(score, 1), strengths, warnings


def score_track_record(m: TraderMetrics) -> tuple[float, list, list]:
    """트랙레코드 점수 (0~100)"""
    strengths, warnings = [], []

    # 활동 일수: 7일 ~ 365일
    days_score = _normalize(m.trading_days, 7, 365)

    # 전체 PnL 부호
    pnl_sign_score = 80 if m.pnl_all_time > 0 else 20

    score = days_score * 0.60 + pnl_sign_score * 0.40

    if m.trading_days > 90:
        strengths.append(f"{m.trading_days}d history — reliable")
    elif m.trading_days < 14:
        warnings.append(f"이력 {m.trading_days}일 미만 (검증 부족)")
    if m.pnl_all_time < 0:
        warnings.append(f"전체 누적 PnL 마이너스 (${m.pnl_all_time:,.0f})")

    return round(score, 1), strengths, warnings


def check_hard_filters(m: TraderMetrics) -> tuple[bool, str]:
    """하드 필터 체크. 반환: (통과여부, 실패사유)"""
    if m.win_rate > 0 and m.win_rate < HARD_FILTER["min_win_rate"]:
        return False, f"승률 {m.win_rate:.1f}% < {HARD_FILTER['min_win_rate']}% 미달"
    if 0 < m.profit_factor < HARD_FILTER["min_profit_factor"]:
        return False, f"Profit Factor {m.profit_factor:.2f} < 1.0 (총손실 > 총수익)"
    if m.max_drawdown > HARD_FILTER["max_drawdown"]:
        return False, f"Max Drawdown {m.max_drawdown:.1f}% > 50% 초과"
    if m.trade_count_30d > 0 and m.trade_count_30d < HARD_FILTER["min_trade_count_30d"]:
        return False, f"30일 거래 {m.trade_count_30d}건 < 5건 (비활성)"
    if m.roi_30d < HARD_FILTER["min_roi_30d"]:
        return False, f"30일 ROI {m.roi_30d:.1f}% < -20% (과도한 손실)"
    if 0 < m.avg_hold_time_min < HARD_FILTER["min_hold_time_min"]:
        return False, f"평균 보유 {m.avg_hold_time_min:.1f}분 < 3분 (복사 불가)"
    if m.max_leverage > HARD_FILTER["max_leverage"]:
        return False, f"최대 레버리지 {m.max_leverage:.0f}x > 20x (초고위험)"
    return True, ""


def determine_tier(score: float, m: TraderMetrics) -> str:
    """티어 결정"""
    for tier_name, criteria in TIER_THRESHOLDS.items():
        if tier_name == "C":
            return "C"
        if (score >= criteria["min_score"] and
                m.max_drawdown <= criteria["max_dd"] and
                m.win_rate >= criteria["min_win_rate"] and
                m.trading_days >= criteria["min_days"]):
            return tier_name
    return "C"


def get_copy_ratio(tier: str, base_ratio: float = 0.05) -> float:
    """티어별 권장 복사 비율"""
    multipliers = {"S": 2.0, "A": 1.4, "B": 1.0, "C": 0.0}
    caps = {"S": 0.10, "A": 0.07, "B": 0.05, "C": 0.0}
    ratio = base_ratio * multipliers.get(tier, 0)
    return min(ratio, caps.get(tier, 0))


def compute_trust_score(m: TraderMetrics) -> TraderScore:
    """전체 Trust Score 계산"""
    score = TraderScore(address=m.address, alias=m.alias)

    # 하드 필터
    passed, reason = check_hard_filters(m)
    if not passed:
        score.disqualified = True
        score.disqualify_reason = reason
        score.tier = "X"
        logger.info(f"[DISQUALIFIED] {m.alias or m.address[:8]}: {reason}")
        return score

    # 5차원 점수
    p_score, p_str, p_warn = score_profitability(m)
    r_score, r_str, r_warn = score_risk(m)
    c_score, c_str, c_warn = score_consistency(m)
    e_score, e_str, e_warn = score_execution(m)
    t_score, t_str, t_warn = score_track_record(m)

    score.profitability_score = p_score
    score.risk_score = r_score
    score.consistency_score = c_score
    score.execution_score = e_score
    score.track_record_score = t_score

    # 가중 합산
    trust = (
        p_score * 0.30 +
        r_score * 0.25 +
        c_score * 0.20 +
        e_score * 0.15 +
        t_score * 0.10
    )
    score.trust_score = round(trust, 1)

    # 티어 + 복사 비율
    score.tier = determine_tier(trust, m)
    score.recommended_copy_ratio = get_copy_ratio(score.tier)

    # 강점/경고 수집
    score.strengths = p_str + r_str + c_str + e_str + t_str
    score.warnings = p_warn + r_warn + c_warn + e_warn + t_warn

    return score


def score_from_api_data(raw: dict) -> TraderScore:
    """
    Pacifica API 응답 데이터 → TraderMetrics → TraderScore
    API 필드: address, pnl_all_time, pnl_30d, pnl_7d, pnl_1d,
              roi_at, roi_30d, roi_7d, equity, oi, consistency
    """
    addr = raw.get("address", "")
    alias = raw.get("alias", addr[:8])

    # equity 기반 max_drawdown 추정 (실데이터 없을 때)
    equity = float(raw.get("equity", 100000) or 100000)
    pnl_at = float(raw.get("pnl_all_time", 0) or 0)
    roi_at = float(raw.get("roi_at", 0) or 0)

    # Max Drawdown 추정: equity 성장과 PnL 기반
    # equity_start = equity - pnl_at
    # max_dd 직접 데이터 없으면 roi 기반 추정
    if roi_at > 0:
        # 수익률이 높을수록 드로다운도 더 있었을 것 (rough estimate)
        estimated_dd = min(abs(roi_at) * 0.3, 45)
    else:
        estimated_dd = min(abs(roi_at) * 0.8, 60)

    # 거래 활성도 (consistency 필드: 1~5 스케일 추정)
    consistency = int(raw.get("consistency", 3) or 3)
    trade_count_est = consistency * 15  # rough proxy

    m = TraderMetrics(
        address=addr,
        alias=alias,
        roi_30d=float(raw.get("roi_30d", 0) or 0),
        roi_7d=float(raw.get("roi_7d", 0) or 0),
        pnl_30d=float(raw.get("pnl_30d", 0) or 0),
        pnl_all_time=pnl_at,
        max_drawdown=estimated_dd,
        current_equity=equity,
        avg_leverage=float(raw.get("avg_leverage", 3.0) or 3.0),
        max_leverage=float(raw.get("max_leverage", 5.0) or 5.0),
        win_rate=float(raw.get("win_rate", 55) or 55),  # API에 없으면 중간값
        trade_count_30d=trade_count_est,
        profit_factor=float(raw.get("profit_factor", 1.5) or 1.5),
        avg_hold_time_min=float(raw.get("avg_hold_min", 120) or 120),
        avg_position_usdc=float(raw.get("avg_position_usdc", 500) or 500),
        trading_days=int(raw.get("trading_days", 30) or 30),
        monthly_positive_rate=float(raw.get("monthly_positive_rate", 60) or 60),
        open_positions=int(raw.get("open_positions", 0) or 0),
    )
    return compute_trust_score(m)


def rank_traders(trader_list: list[dict]) -> list[dict]:
    """
    트레이더 목록 → Trust Score 계산 후 순위 정렬
    반환: [{"address", "alias", "tier", "trust_score", "copy_ratio", ...}, ...]
    """
    results = []
    for raw in trader_list:
        score = score_from_api_data(raw)
        if not score.disqualified:
            d = score.to_dict()
            d["metrics_summary"] = {
                "roi_30d": raw.get("roi_30d"),
                "pnl_30d": raw.get("pnl_30d"),
                "equity": raw.get("equity"),
            }
            results.append(d)

    results.sort(key=lambda x: x["trust_score"], reverse=True)
    logger.info(f"[Scorer] {len(results)}명 평가 완료 (제외: {len(trader_list)-len(results)}명)")
    return results


# ── 테스트 ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import json

    # 메인넷 실제 데이터로 테스트
    test_traders = [
        {
            "address": "HTWWhKsLumaYZ5DCLZfLG4XtmcSo7LBjAx9PSvYMZLY6",
            "alias": "HTW-TOP1",
            "pnl_all_time": 36191,
            "pnl_30d": 46485,
            "pnl_7d": 32202,
            "roi_at": 24.76,
            "roi_30d": 31.80,
            "roi_7d": 22.03,
            "equity": 146191,
            "consistency": 4,
            "win_rate": 68,
            "profit_factor": 2.8,
            "trading_days": 45,
        },
        {
            "address": "5C9GKLrKFUvLWZEbMZQC5mtkTdKxuUhCzVCXZQH4FmCw",
            "alias": "5C9-TOP2",
            "pnl_all_time": 145785,
            "pnl_30d": 105373,
            "pnl_7d": 2531,
            "roi_at": 57.00,
            "roi_30d": 41.20,
            "roi_7d": 0.99,
            "equity": 255785,
            "consistency": 3,
            "win_rate": 62,
            "profit_factor": 2.2,
            "trading_days": 60,
        },
        {
            "address": "YjCD9Gek6MVY9t3MLEGYYdZLeaF6MZrpgZraayWsv9E",
            "alias": "YjCD-TOP3",
            "pnl_30d": 119428,
            "roi_30d": 12.26,
            "equity": 200000,
            "win_rate": 90,
            "profit_factor": 1422059,  # 이상치
            "consistency": 5,
            "trading_days": 35,
        },
        {
            "address": "GTU92nBC8LMyt9W4Qqc319BFR1vpkNNPAbt4QCnX7kZ6",
            "alias": "GTU-BAD",
            "pnl_30d": -80000,
            "roi_30d": -86,
            "equity": 50000,
            "win_rate": 15,
            "profit_factor": 0.11,
            "trading_days": 30,
        },
    ]

    print("=" * 60)
    print("트레이더 신뢰도 점수 평가 결과")
    print("=" * 60)
    ranked = rank_traders(test_traders)

    for i, r in enumerate(ranked, 1):
        print(f"\n#{i} [{r['tier']}] {r['alias']} — Trust: {r['trust_score']:.1f}/100")
        print(f"  수익성: {r['profitability_score']:.1f} | 위험: {r['risk_score']:.1f} | "
              f"일관성: {r['consistency_score']:.1f} | 실행: {r['execution_score']:.1f} | "
              f"이력: {r['track_record_score']:.1f}")
        print(f"  권장 복사비율: {r['recommended_copy_ratio']*100:.1f}%")
        if r["strengths"]:
            print(f"  ✅ {' | '.join(r['strengths'][:2])}")
        if r["warnings"]:
            print(f"  ⚠️  {' | '.join(r['warnings'][:2])}")

    # 제외된 트레이더도 출력
    print("\n[제외 트레이더]")
    for raw in test_traders:
        s = score_from_api_data(raw)
        if s.disqualified:
            print(f"  ❌ {raw.get('alias', raw['address'][:8])}: {s.disqualify_reason}")

    # JSON 저장
    output = {"scored_traders": ranked}
    with open("trader_scores.json", "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\n결과 저장: trader_scores.json")
