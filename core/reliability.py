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
import re as _re
from dataclasses import dataclass, field, asdict
from typing import Optional


# ────────────────────────────────────────────
# alias 정규화 유틸
# ────────────────────────────────────────────
def _normalize_alias(alias: str, address: str = "") -> str:
    """
    트레이더 alias 정규화 (R8)
    - 특수문자 strip: "---- Donk ----" → "Donk"
    - 길이 제한: 최대 20자
    - 빈 결과 → address[:8] fallback
    """
    if not alias:
        return address[:8] if address else "Unknown"
    # 선행/후행 특수문자(대시, 점, 공백, 별표 등) 제거
    cleaned = _re.sub(r'^[\s\-_\*\.~=|]+|[\s\-_\*\.~=|]+$', '', alias)
    # 내부 연속 공백 → 단일 공백
    cleaned = _re.sub(r'\s{2,}', ' ', cleaned)
    # 제어 문자 제거
    cleaned = _re.sub(r'[\x00-\x1f\x7f]', '', cleaned)
    # 최대 20자 제한
    cleaned = cleaned[:20].strip()
    # 빈 문자열이면 address fallback
    if not cleaned:
        return address[:8] if address else "Unknown"
    return cleaned


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
    """0~100 정규화, 항상 클램핑 보장. NaN/Inf 입력 시 중립값(50) 반환."""
    import math as _math
    if _math.isnan(v) or _math.isinf(v):
        return 50.0
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
        warnings.append(f"7d loss vs 30d ({m7_30:.0%})")
    elif m7_30 < 0.05:
        ratio_score = 20.0
        warnings.append(f"Low recent activity ({m7_30:.1%} of 30d)")
    elif m7_30 > 0.80:
        ratio_score = 55.0
        warnings.append(f"One-time spike suspected ({m7_30:.0%} in 7d)")
    else:
        # 이상적 범위 0.15~0.45에서 100점
        ratio_score = _norm(abs(m7_30 - 0.30), 0, 0.25, invert=True)

    # 1일 모멘텀 가점/감점 (최근 하루 방향)
    if p1 > 0:
        day_bonus = min(15.0, p1 / max(p30, 1) * 100)
    elif p1 < 0:
        day_bonus = max(-20.0, p1 / max(p30, 1) * 100)
        if p1 < -p30 * 0.05:
            warnings.append(f"Today loss ${p1:,.0f}")
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

    # R8: ROI 점수 개선 — 고ROI 트레이더 역상관 문제 수정
    # 기존: ROI>200% → 40점 고착 (단발 투기 의심 패널티 과도)
    # 수정: ROI 범위를 더 세밀하게 분리, 하드필터(300%)와 중복 패널티 제거
    if roi30 > 300:
        # 하드필터에서 이미 걸러지므로 여기 도달하면 예외적 케이스만
        roi_score = 50.0
        warnings.append(f"ROI {roi30:.0f}% — extreme (passed hard filter)")
    elif roi30 > 150:
        roi_score = 75.0   # 150~300%: 훌륭하지만 재현성 의문
        warnings.append(f"ROI {roi30:.0f}% — very high, verify consistency")
    elif roi30 > 100:
        roi_score = 85.0   # 100~150%: 우수
    elif roi30 > 50:
        roi_score = 90.0   # 50~100%: 최적 범위
    elif roi30 >= 20:
        roi_score = _norm(roi30, 20, 50)
        roi_score = max(70.0, roi_score)
    elif roi30 >= 5:
        roi_score = _norm(roi30, 5, 20)
    elif roi30 >= 0:
        roi_score = _norm(roi30, 0, 5)
    else:
        roi_score = 0.0

    # Profit Factor (개별 거래 데이터 있을 때)
    pf = stats.get("profit_factor", 0)
    if pf and pf > 0:
        pf_capped = min(pf, 5.0)
        # log 인수 보장: pf_capped > 0 이므로 안전 (+ 0.01은 pf=0 방어용이지만 이미 pf>0 체크)
        pf_score = _norm(math.log(max(pf_capped, 1e-9) + 0.01),
                         math.log(0.5 + 0.01), math.log(5.0 + 0.01))
        if pf > 100:
            warnings.append(f"PF {pf:.0f} — outlier suspected")
        elif pf > 2.0:
            pass  # strength
        elif pf < 1.2:
            warnings.append(f"PF {pf:.2f} — low")
    else:
        pf_score = 50.0  # 데이터 없으면 중립

    # Expectancy (거래당 기대값)
    exp = stats.get("expectancy", 0)
    if exp > 0:
        exp_score = min(100.0, _norm(exp, 0, 500))
    elif exp < 0:
        exp_score = 0.0
        warnings.append(f"Negative EV ${exp:.2f} per trade")
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

    P0 Fix (Round 4):
    - OI=0 트레이더: 현재 오픈 포지션 없음 = 리스크 낮지만 비활성 상태
      → risk_score를 100(최고)으로 주지 않고 중립값 50으로 처리
      → 실제 모멘텀/일관성 점수에서 패널티를 받도록 설계 (각 차원 역할 유지)
    - stats 없을 때 OI=0이면 score=50(중립), OI>0이면 oi_score 그대로 사용
    """
    warnings = []

    # OI/Equity 비율 (레버리지 proxy)
    oi_ratio = oi / eq if eq > 0 else 0

    # P0 Fix: OI=0 → 포지션 없음 → risk 판단 불가 → 중립(50)
    if oi == 0:
        oi_score = 50.0  # 중립: 위험하지도 않지만 활동 데이터 없음
        warnings.append("OI=0 — no open positions (neutral risk)")
    elif oi_ratio > 3.0:
        oi_score = 0.0
        warnings.append(f"OI/Equity {oi_ratio:.1f}x — extreme leverage")
    elif oi_ratio > 1.5:
        oi_score = _norm(oi_ratio, 1.5, 3.0, invert=True) * 0.5
        warnings.append(f"OI/Equity {oi_ratio:.1f}x — high risk")
    else:
        oi_score = _norm(oi_ratio, 0, 1.5, invert=True)

    # 청산 비율 (있으면)
    liq_rate = stats.get("liquidation_rate", 0)
    if liq_rate > 0.05:
        liq_score = 0.0
        warnings.append(f"Liq rate {liq_rate:.1%} — unreliable")
    elif liq_rate > 0.01:
        liq_score = 30.0
        warnings.append(f"Liq rate {liq_rate:.1%}")
    else:
        liq_score = 100.0

    # 최대 연속 손실
    streak = stats.get("max_consecutive_loss", 0)
    streak_score = _norm(streak, 0, 10, invert=True)
    if streak > 5:
        warnings.append(f"Max losing streak {streak}")

    if stats:
        score = oi_score * 0.45 + liq_score * 0.30 + streak_score * 0.25
    else:
        # stats 없을 때: OI=0이면 중립(50), 아니면 oi_score 그대로
        score = oi_score
    return round(score, 1), warnings


def _score_consistency(cons: int, p30: float, p7: float, stats: dict,
                        raw: dict | None = None) -> tuple[float, list]:
    """
    일관성 신뢰성 (15%)
    활동의 규칙성 — 꾸준히 매매하는가

    R8 Fix: 3개 고유값 고착 문제 해결
    - equity 변동성(pnl 분포), 최대 낙폭, 연속손실 추가 지표 활용
    - pnl_7d/pnl_30d 비율 외에 pnl_1d / pnl_30d 단기 일관성 추가
    - 0~100 full range 활용하도록 세분화
    """
    warnings = []

    # API의 consistency 필드 (1~5 스케일 추정) — 기본 기여
    cons_score = _norm(cons, 1, 5)
    if cons <= 1:
        warnings.append("Very low consistency (irregular trading)")

    # 거래 빈도 (있으면) — None 방어
    tc = stats.get("trade_count") or 0
    if tc > 0:
        if 10 <= tc <= 200:
            freq_score = 100.0
        elif tc < 5:
            freq_score = 20.0
            warnings.append(f"{tc} trades — insufficient sample")
        else:
            freq_score = _norm(tc, 200, 500, invert=True)
    else:
        freq_score = 50.0

    # 평균 보유 시간 (복사 가능성)
    # R10: hold_min이 stats에 명시적으로 있는지 확인 (추정값 vs 실제값)
    hold = stats.get("avg_hold_min", None)
    _hold_is_estimated = hold is None  # stats에 없으면 추정값 사용
    if hold is None:
        hold = 60.0  # 기본값 (데이터 없음)

    if hold < 3:
        hold_score = 0.0
        warnings.append(f"Avg hold {hold:.1f}min — uncopyable")
    elif hold < 10:
        hold_score = 40.0
        warnings.append(f"Avg hold {hold:.1f}min — slippage risk")
    elif hold < 60:
        # 10~60분: 복사 가능하지만 슬리피지 주의
        hold_score = _norm(hold, 10, 60) * 0.6 + 40.0  # 40~64점
        hold_score = min(64.0, hold_score)
    elif hold <= 480:
        # 60~480분(1~8시간): 이상적 범위 → 64~100점
        hold_score = _norm(hold, 60, 480) * 0.36 + 64.0
        hold_score = min(100.0, hold_score)
    else:
        # 480분 초과: 장기 보유 (스윙) → 복사는 가능하지만 드물게 거래
        hold_score = 90.0

    # stats에서 직접 나온 hold_min이면 반영, 추정값이면 가중치 감소
    if _hold_is_estimated:
        hold_score = hold_score * 0.5 + 50.0 * 0.5  # 중립 쪽으로 당김

    # R8 추가: 최대 연속 손실 기반 안정성
    max_streak = stats.get("max_consecutive_loss", 0)
    if max_streak == 0:
        streak_bonus = 15.0   # 연속손실 0 → 보너스
    elif max_streak <= 2:
        streak_bonus = 10.0
    elif max_streak <= 5:
        streak_bonus = 0.0
    else:
        streak_bonus = -15.0  # 연속손실 많으면 패널티
        warnings.append(f"Max streak {max_streak} losses — volatile")

    # R8 추가: 단기/중기 수익 일관성 (pnl_1d 활용)
    # pnl_1d > 0이면 최근에도 수익 → 추가 포인트
    p1 = _safe((raw or {}).get("pnl_1d")) if raw else 0.0
    if p30 > 0:
        daily_ratio = p1 / p30
        if 0.01 <= daily_ratio <= 0.15:   # 오늘이 30일의 1~15% 기여 (이상적)
            daily_bonus = 10.0
        elif daily_ratio > 0:
            daily_bonus = 5.0
        elif daily_ratio < -0.10:          # 오늘 크게 손실
            daily_bonus = -10.0
            warnings.append(f"Today loss impact {daily_ratio:.0%}")
        else:
            daily_bonus = 0.0
    else:
        daily_bonus = 0.0

    # R8 추가: 7d/30d PnL 비율로 지속성 평가 (더 세밀하게)
    if p30 > 0 and p7 >= 0:
        ratio_7_30 = p7 / p30
        if 0.10 <= ratio_7_30 <= 0.50:   # 7일이 30일의 10~50% 기여
            ratio_bonus = 15.0
        elif ratio_7_30 > 0.50:
            ratio_bonus = 5.0             # 집중도 높음
        elif ratio_7_30 > 0:
            ratio_bonus = 8.0
        else:
            ratio_bonus = -5.0
    elif p30 > 0 and p7 < 0:
        ratio_bonus = -10.0
        warnings.append("7d PnL negative vs 30d")
    else:
        ratio_bonus = 0.0

    if stats:
        base = cons_score * 0.25 + freq_score * 0.30 + hold_score * 0.30
        # 보너스/패널티 합산 후 0~100 클램핑 (최대 15점 추가 가능)
        bonus = streak_bonus * 0.25 + daily_bonus * 0.25 + ratio_bonus * 0.20
        score = max(0.0, min(100.0, base + bonus))
    else:
        # stats 없으면 cons_score + ratio/daily 보너스만
        base = cons_score
        bonus = (streak_bonus + daily_bonus + ratio_bonus) * 0.15
        score = max(0.0, min(100.0, base + bonus))
    return round(score, 1), warnings


def _score_copyability(stats: dict, p30: float) -> tuple[float, list]:
    """
    복사 가능성 (5%)
    팔로워가 실제로 이 트레이더를 따라갈 수 있는가
    (포지션 크기, 보유시간, 승률, 거래 빈도 기반)

    R8 Fix: win_rate / trade_count 기반 실계산 추가
    - stats 없으면 50.0 중립 반환 (60.0 고착 제거)
    - win_rate가 있으면 복사 신뢰성 점수에 반영
    - trade_count가 높을수록 복사 용이(충분한 샘플)
    """
    warnings = []

    # stats 없으면 중립값 반환 (60.0 고착 → 50.0으로 수정)
    if not stats:
        return 50.0, ["No trade data — neutral copyability"]

    avg_pos = stats.get("avg_position_usdc", 0)
    hold    = stats.get("avg_hold_min", 60)
    win_rate = stats.get("win_rate", None)   # 0~1 스케일
    trade_count = stats.get("trade_count") or 0

    # 포지션 크기: $50~$5000이 이상적
    if 50 <= avg_pos <= 5000:
        pos_score = 100.0
    elif avg_pos < 10:
        pos_score = 20.0
        warnings.append(f"Avg pos ${avg_pos:.0f} — too small")
    elif avg_pos > 50000:
        pos_score = 30.0
        warnings.append(f"Avg pos ${avg_pos:,.0f} — severe slippage")
    else:
        pos_score = 70.0

    # 보유시간 기반 복사 가능성
    if hold < 3:
        hold_score = 0.0
    elif hold < 15:
        hold_score = 50.0
    else:
        hold_score = 100.0

    # win_rate 기반 복사 신뢰성 (0~1 스케일)
    if win_rate is not None and win_rate > 0:
        # 승률 45%~75% → 30~100점 (너무 높으면 이상치 의심)
        if win_rate > 0.90:
            wr_score = 60.0   # 극단적 승률은 의심
            warnings.append(f"Win rate {win_rate:.0%} — suspiciously high")
        elif win_rate >= 0.55:
            wr_score = _norm(win_rate, 0.55, 0.80)
            wr_score = max(70.0, wr_score)
        elif win_rate >= 0.40:
            wr_score = _norm(win_rate, 0.40, 0.55)
        else:
            wr_score = max(0.0, win_rate * 100)
            warnings.append(f"Win rate {win_rate:.0%} — low copyability")
    else:
        wr_score = 50.0   # 데이터 없으면 중립

    # 거래 빈도 (샘플 충분성): 거래가 많을수록 복사 용이
    if trade_count >= 30:
        tc_score = 100.0
    elif trade_count >= 10:
        tc_score = _norm(trade_count, 10, 30)
        tc_score = max(60.0, tc_score)
    elif trade_count >= 3:
        tc_score = _norm(trade_count, 3, 10)
        tc_score = max(30.0, tc_score)
    elif trade_count > 0:
        tc_score = 20.0
        warnings.append(f"{trade_count} trades — small sample")
    else:
        tc_score = 50.0   # 중립

    # 가중 합산: 포지션크기 30% + 보유시간 25% + 승률 30% + 거래빈도 15%
    score = pos_score * 0.30 + hold_score * 0.25 + wr_score * 0.30 + tc_score * 0.15
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

    # strength / 경고
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
    # R8: alias 정규화 — "---- Donk ----" 같은 이상한 형태 정리
    alias = _normalize_alias(raw.get("alias", "") or "", addr)

    p30   = _safe(raw.get("pnl_30d"))
    p7    = _safe(raw.get("pnl_7d"))
    p1    = _safe(raw.get("pnl_1d"))
    p_at  = _safe(raw.get("pnl_all_time"))
    roi30 = _safe(raw.get("roi_30d"))
    # equity: equity_current(최신) > equity 순으로 우선 사용
    eq    = _safe(raw.get("equity_current") or raw.get("equity") or 100000)
    # oi: oi_current(최신) > oi 순으로 우선 사용
    # DB에 구버전 oi 컬럼과 oi_current 컬럼이 공존 — 항상 최신값 우선
    oi    = _safe(raw.get("oi_current") or raw.get("oi"))
    # P0 Fix (Round 4): consistency 기본값 3 고착 문제
    # Pacifica API가 consistency 필드를 제공하지 않으면 leaderboard 데이터로 추정
    _raw_cons = raw.get("consistency")
    if _raw_cons is not None:
        cons = int(_safe(_raw_cons))
    else:
        # API가 consistency 미제공 시: p7d/p30d 비율로 1~5 추정
        # - 최근 7일 활동 + 30일 일관성 기반 proxy
        _p30_v = _safe(raw.get("pnl_30d"))
        _p7_v  = _safe(raw.get("pnl_7d"))
        _p1_v  = _safe(raw.get("pnl_1d"))
        if _p30_v > 0 and _p7_v > 0:
            # 7일 기여율: 이상적 0.15~0.45 → 일관성 4~5
            _ratio = _p7_v / _p30_v
            if 0.10 <= _ratio <= 0.50:
                cons = 4
            elif _ratio > 0 and _ratio < 0.10:
                cons = 3   # 7일 기여 낮음 (비활성 경향)
            elif _ratio > 0.50:
                cons = 3   # 단발성 (한 주에 집중)
            elif _ratio < 0:
                cons = 2   # 최근 손실 (하락 전환)
            else:
                cons = 3
        elif _p30_v > 0 and _p7_v == 0:
            cons = 2       # 30일 수익 있지만 최근 7일 활동 없음
        elif _p1_v > 0:
            cons = 3       # 오늘만 수익
        else:
            cons = 2       # 기본 낮음 (데이터 불충분)

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
        reasons.append(f"Assets ${eq:,.0f} < ${HARD_FILTER['min_equity_usdc']:,}")
    if p30 < HARD_FILTER["min_pnl_30d"]:
        reasons.append(f"30d PnL ${p30:,.0f} < ${HARD_FILTER['min_pnl_30d']:,}")
    if oi_ratio > HARD_FILTER["max_oi_equity_ratio"]:
        reasons.append(f"OI/Equity {oi_ratio:.1f}x > {HARD_FILTER['max_oi_equity_ratio']}x")
    if cons < HARD_FILTER["min_consistency"]:
        reasons.append(f"Consistency {cons} < {HARD_FILTER['min_consistency']}")
    if p30 > 0 and mom_ratio < HARD_FILTER["min_momentum_ratio"]:
        reasons.append(f"Recent decline {mom_ratio:.0%} (>-30% vs 30d)")
    if roi30 > HARD_FILTER["max_roi_30d"]:
        reasons.append(f"ROI {roi30:.0f}% — spike suspected")

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
    else:
        # trades/history API 없을 때: DB 컬럼 기반 간이 stats 구성
        # win_rate, total_trades 컬럼이 DB에 존재하면 활용
        _db_wr = raw.get("win_rate")  # DB traders.win_rate (0~1 or 0~100)
        _db_tt = raw.get("total_trades", 0)
        _db_wc = raw.get("win_count", 0)
        _db_lc = raw.get("lose_count", 0)
        _vol30 = _safe(raw.get("volume_30d"))  # 30일 거래량 (USD)

        # R10: volume_30d + win/lose count 기반 avg_position_usdc 추정
        _est_trade_count = None
        _est_avg_pos = 0.0
        _est_hold_min = None  # None=추정 불가

        # total_trades가 실제로 채워진 경우
        _tt_val = int(_db_tt or 0)
        _wc_val = int(_db_wc or 0)
        _lc_val = int(_db_lc or 0)
        _total_from_wl = _wc_val + _lc_val

        if _total_from_wl > 0:
            _est_trade_count = _total_from_wl
        elif _tt_val > 0:
            _est_trade_count = _tt_val

        # avg_position_usdc 추정: volume_30d / trade_count
        if _vol30 > 0 and _est_trade_count and _est_trade_count > 0:
            _raw_pos = _vol30 / _est_trade_count
            # 포지션이 equity 대비 극단적이면 보정 (최대 equity의 50%)
            _est_avg_pos = min(_raw_pos, eq * 0.50) if eq > 0 else _raw_pos
        elif _vol30 > 0 and eq > 0:
            # trade_count 없을 때: equity 기반 보수적 추정
            # volume_30d > equity*10 인 경우 고레버리지 → 포지션 추정 어려움
            _vol_ratio = _vol30 / eq
            if _vol_ratio > 10:
                # 고레버리지 트레이더: equity의 20% 추정 (슬리피지 위험 반영)
                _est_avg_pos = min(eq * 0.20, 10000.0)
            else:
                # 일반 케이스: equity의 10~30% 추정
                _est_avg_pos = min(eq * min(0.30, _vol_ratio / 30 + 0.05), 5000.0)
        elif eq > 0:
            # 완전 데이터 없을 때: equity의 10% 를 기본 포지션으로
            _est_avg_pos = min(eq * 0.10, 5000.0)

        # R10: OI + volume_7d 기반 avg_hold_min 추정 (더 보수적)
        # volume_7d 우선 사용 (더 최신), 없으면 volume_30d/4
        _vol7 = _safe(raw.get("volume_7d"))
        _vol_recent = _vol7 if _vol7 > 0 else (_vol30 / 4 if _vol30 > 0 else 0)

        if oi > 0 and _vol_recent > 0:
            # OI/vol_7d ≈ 현재 포지션 / 최근 1주일 거래량
            # hold_min ≈ (OI/vol_7d) * 7일 * 24h * 60min
            # 단, oi는 current 가격이라 변동 크므로 min(oi, eq*2)로 캡핑
            _oi_capped = min(oi, eq * 2.0) if eq > 0 else oi
            _hold_est = (_oi_capped / _vol_recent) * 7 * 24 * 60
            _est_hold_min = round(min(max(5.0, _hold_est), 2880.0), 1)  # 5분~2일
        elif oi > 0 and eq > 0:
            # volume 없으면: OI/equity 비율로 추정
            _oi_ratio = min(oi, eq * 3) / eq
            if _oi_ratio > 1.0:
                _est_hold_min = 60.0   # 고레버리지 → 빠른 회전 예상
            elif _oi_ratio > 0.3:
                _est_hold_min = 240.0  # 중간
            else:
                _est_hold_min = 720.0  # 소규모 포지션 → 스윙
        elif oi == 0 and _vol30 > 0 and eq > 0:
            # OI=0(포지션 없음)이지만 거래는 있음
            _vol_eq = _vol30 / eq if eq > 0 else 0
            if _vol_eq > 100:
                _est_hold_min = 15.0   # 고빈도 스캘핑
            elif _vol_eq > 10:
                _est_hold_min = 60.0   # 중빈도
            else:
                _est_hold_min = 240.0  # 저빈도 스윙

        # win_rate=0.0 이어도 win_count+lose_count > 0 이면 실제 데이터 존재
        _has_real_wr = (
            _db_wr is not None and
            (_total_from_wl > 0 or float(_db_wr) > 0)  # 거래 이력 존재
        )
        if _has_real_wr:
            # DB win_rate가 0~100 스케일이면 0~1로 정규화
            wr_norm = float(_db_wr) / 100.0 if float(_db_wr) > 1 else float(_db_wr)
            stats = {
                "win_rate": round(wr_norm, 4),
                "trade_count": _est_trade_count or int(_db_tt or 0),
                "win_count": _wc_val if _wc_val > 0 else None,
                "lose_count": _lc_val if _lc_val > 0 else None,
                "avg_position_usdc": round(_est_avg_pos, 2),
                "win_rate_source": "db_column",
            }
            if _est_hold_min is not None:
                stats["avg_hold_min"] = _est_hold_min
        elif roi30 > 0 and cons >= 2:
            # 완전 데이터 없을 때: roi+consistency 기반 추정 win_rate
            # 보수적으로 추정 (50% 기준 ± 조정)
            estimated_wr = 0.50
            estimated_wr += max(-0.15, min(0.15, roi30 / 1000))  # ROI 기여 최대 ±15%
            estimated_wr += (cons - 3) * 0.04                     # consistency 기여 ±8%
            # R10: momentum 기반 추정 win_rate 보정
            if p30 > 0 and p7 > 0:
                _m_ratio = p7 / p30
                estimated_wr += max(-0.05, min(0.05, (_m_ratio - 0.25) * 0.2))
            estimated_wr = round(max(0.30, min(0.80, estimated_wr)), 4)
            stats = {
                "win_rate": estimated_wr,
                "trade_count": _est_trade_count,
                "avg_position_usdc": round(_est_avg_pos, 2),
                "win_rate_source": "estimated",  # 추정값 명시
            }
            if _est_hold_min is not None:
                stats["avg_hold_min"] = _est_hold_min
        result.trade_stats = stats

    # ── 5차원 점수 ─────────────────────────────────
    m_score, m_warn = _score_momentum(p30, p7, p1, roi30)
    p_score, p_warn = _score_profitability(p30, roi30, p7, stats)
    r_score, r_warn = _score_risk(eq, oi, roi30, stats)
    # R8: raw 전달 → pnl_1d 등 추가 지표 활용
    c_score, c_warn = _score_consistency(cons, p30, p7, stats, raw)
    cp_score, cp_warn = _score_copyability(stats, p30)

    result.momentum_score      = m_score
    result.profitability_score = p_score
    result.risk_score          = r_score
    result.consistency_score   = c_score
    result.copyability_score   = cp_score

    # R8: 가중치 재조정
    # 기존: momentum 25% / profitability 30% / risk 25% / consistency 15% / copyability 5%
    # 변경: momentum 20% / profitability 30% / risk 25% / consistency 17% / copyability 8%
    # 근거: momentum_score 과지배 방지, copyability/consistency 실데이터 기반 강화
    crs = (
        m_score  * 0.20 +
        p_score  * 0.30 +
        r_score  * 0.25 +
        c_score  * 0.17 +
        cp_score * 0.08
    )
    result.crs = round(crs, 1)

    # ── 등급 ─────────────────────────────────────
    for g, threshold in GRADE.items():
        if crs >= threshold:
            result.grade = g
            break
    result.recommended_copy_ratio = MAX_COPY_RATIO[result.grade]

    # ── strength / 경고 수집 ──────────────────────────
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
