"""
scorer.py — CRS 신뢰성 점수 계산 (에이전트가 반복 수정하는 파일)

AutoResearch 루프:
  evaluate.py가 이 파일을 import → score() 호출 → follower_loss 반환
  에이전트가 가중치/임계값 수정 → evaluate → 개선 시 commit

수정 가능: w_* (가중치), threshold_* (임계값), bonus/penalty 로직
수정 불가: 함수 시그니처, import 구조
"""

import math
from typing import Optional

# ══════════════════════════════════════════════════════
# ▶ 에이전트 수정 구역 — 가중치 & 임계값
# ══════════════════════════════════════════════════════

# [수익성] 가중치
w_profit_factor   = 0.10   # Profit Factor
w_sharpe          = 0.19   # Sharpe Ratio
w_ept_net         = 0.35   # EPT_net (팔로워 비용 차감 후 기대수익) ← 핵심

# [위험조정] 가중치
w_sortino         = 0.11   # Sortino Ratio
w_csr             = 0.08   # Common Sense Ratio
w_recovery        = 0.07   # Recovery Factor

# [위험] 가중치
w_mdd             = 0.01   # Max Drawdown (역수)
w_ror             = 0.04   # Risk of Ruin (역수)
w_mcl             = 0.03   # Max Consecutive Loss (역수)

# [전략 순도] 가중치
w_purity          = 0.15   # Strategy Purity (펀딩비 필터 핵심)
w_sample          = 0.02   # 표본 수 (충분성)
w_freq_penalty    = 0.02   # 고빈도 슬리피지 패널티 (실험적)

# ── 임계값 ─────────────────────────────────────────────

# 하드 필터 (이 조건 위반 시 score=0, 복사 금지)
threshold_min_ept_net      = 0.0      # 팔로워 기대수익 최소값 ($)
threshold_min_purity       = 0.25    # 전략 순도 최소 (펀딩비 필터)
threshold_max_mdd          = 0.40    # 최대 드로우다운 40%
threshold_min_trades       = 10      # 최소 표본 수
threshold_min_profit_factor = 1.0   # 최소 Profit Factor

# 소프트 필터 (점수 감점)
threshold_high_freq_trades = 200     # 이 이상이면 슬리피지 패널티
threshold_freq_penalty_rate = 0.10  # 고빈도 패널티 비율

# Kelly 승수 (복사비율 산정)
kelly_fraction             = 0.25   # 풀 Kelly의 1/4 (안전 마진)
max_copy_ratio             = 0.20   # 최대 복사비율 20%

# 팔로워 복사 비용 (슬리피지 + 수수료)
copy_cost_pct              = 0.0011  # 0.11% per trade

# ══════════════════════════════════════════════════════
# ▶ 점수 계산 (수정 금지)
# ══════════════════════════════════════════════════════

def _safe(v, default=0.0, cap=None):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return default
    v = float(v)
    if cap is not None:
        v = min(v, cap)
    return v


def score(metrics: dict) -> dict:
    """
    트레이더 지표 dict → CRS 점수 + 복사 권고

    Args:
        metrics: {
            profit_factor, sharpe, sortino, csr, recovery_factor,
            mdd, risk_of_ruin, max_consec_loss, purity, sample_size,
            avg_win, avg_loss, win_rate, total_trades, avg_trade_interval_s,
            ept_gross  (선택 — 없으면 avg_win/loss로 계산)
        }

    Returns: {
        crs_score (0-100), grade, copy_ratio, follower_loss,
        flags, breakdown
    }
    """
    m = metrics

    # ── 팔로워 EPT_net 계산 ──────────────────────────────
    ept_gross = _safe(m.get("ept_gross"))
    if ept_gross == 0:
        win_rate  = _safe(m.get("win_rate", 0)) / 100
        avg_win   = _safe(m.get("avg_win", 0))
        avg_loss  = abs(_safe(m.get("avg_loss", 0)))
        ept_gross = win_rate * avg_win - (1 - win_rate) * avg_loss

    avg_pos     = _safe(m.get("avg_position_usd", 100))
    ept_net     = ept_gross - avg_pos * copy_cost_pct

    # ── 하드 필터 ────────────────────────────────────────
    flags       = []
    disqualified = False

    pf          = _safe(m.get("profit_factor", 0))
    purity      = _safe(m.get("purity", 1.0))
    mdd         = _safe(m.get("mdd", 0))
    sample_size = int(_safe(m.get("sample_size", m.get("total_trades", 0))))

    if ept_net <= threshold_min_ept_net:
        flags.append(f"❌ 팔로워 기대수익 음수 (EPT_net={ept_net:.4f})")
        disqualified = True

    if purity < threshold_min_purity:
        flags.append(f"❌ 펀딩비/보유 전략 의심 (purity={purity:.2f})")
        disqualified = True

    if mdd > threshold_max_mdd:
        flags.append(f"❌ MDD 과다 ({mdd*100:.1f}%)")
        disqualified = True

    if sample_size < threshold_min_trades:
        flags.append(f"❌ 표본 부족 ({sample_size}건)")
        disqualified = True

    if pf < threshold_min_profit_factor:
        flags.append(f"❌ Profit Factor 미달 ({pf:.2f})")
        disqualified = True

    if disqualified:
        return {
            "crs_score":    0.0,
            "grade":        "F",
            "copy_ratio":   0.0,
            "follower_loss": 999.0,
            "flags":        flags,
            "ept_net":      ept_net,
            "breakdown":    {},
        }

    # ── 소프트 점수 계산 ─────────────────────────────────
    def norm(v, good, bad, cap=10):
        """v를 [0,1]로 정규화. good방향 = 1, bad방향 = 0"""
        v = _safe(v, default=bad, cap=cap if good > bad else None)
        if good == bad:
            return 0.5
        ratio = (v - bad) / (good - bad)
        return max(0.0, min(1.0, ratio))

    sharpe   = _safe(m.get("sharpe", 0), cap=5)
    sortino  = _safe(m.get("sortino", 0), cap=10)
    csr      = _safe(m.get("csr", 0), cap=50)
    rf       = _safe(m.get("recovery_factor", 0), cap=20)
    ror      = _safe(m.get("risk_of_ruin", 1.0))
    mcl      = _safe(m.get("max_consec_loss", 10))

    s_pf        = norm(pf, 3.0, 1.0, cap=10)         * w_profit_factor
    s_sharpe    = norm(sharpe, 3.0, 0.0)              * w_sharpe
    s_ept       = norm(ept_net, 5.0, 0.0, cap=50)     * w_ept_net
    s_sortino   = norm(sortino, 5.0, 0.0)             * w_sortino
    s_csr       = norm(csr, 20.0, 1.0)                * w_csr
    s_rf        = norm(rf, 10.0, 0.0)                 * w_recovery
    s_mdd       = norm(1 - mdd, 1.0, 0.6)             * w_mdd
    s_ror       = norm(1 - ror, 1.0, 0.5)             * w_ror
    s_mcl       = norm(1 / max(mcl, 1), 1.0, 0.1)    * w_mcl
    s_purity    = norm(purity, 1.0, threshold_min_purity) * w_purity
    s_sample    = norm(min(sample_size, 500), 200, threshold_min_trades) * w_sample

    # 고빈도 패널티
    total_trades = int(_safe(m.get("total_trades", 0)))
    freq_penalty = 0.0
    if total_trades > threshold_high_freq_trades and w_freq_penalty > 0:
        excess = (total_trades - threshold_high_freq_trades) / threshold_high_freq_trades
        freq_penalty = min(excess * threshold_freq_penalty_rate, 0.15)
        flags.append(f"⚠️ 고빈도 슬리피지 위험 ({total_trades}건, 패널티={freq_penalty:.2f})")

    raw = (s_pf + s_sharpe + s_ept + s_sortino + s_csr + s_rf
           + s_mdd + s_ror + s_mcl + s_purity + s_sample)
    raw = max(0.0, raw - freq_penalty)
    crs = round(raw * 100, 2)

    # ── 등급 & 복사비율 ──────────────────────────────────
    if   crs >= 75: grade = "A"
    elif crs >= 60: grade = "B"
    elif crs >= 45: grade = "C"
    elif crs >= 30: grade = "D"
    else:           grade = "F"

    win_rate_dec = _safe(m.get("win_rate", 50)) / 100
    avg_win_v    = _safe(m.get("avg_win", 1))
    avg_loss_v   = abs(_safe(m.get("avg_loss", 1), default=1))
    odds         = avg_win_v / max(avg_loss_v, 0.001)
    kelly        = win_rate_dec - (1 - win_rate_dec) / max(odds, 0.001)
    kelly        = max(0.0, kelly)
    copy_ratio   = min(kelly * kelly_fraction, max_copy_ratio)

    # 등급별 cap
    grade_cap    = {"A": 0.20, "B": 0.15, "C": 0.10, "D": 0.05, "F": 0.0}
    copy_ratio   = min(copy_ratio, grade_cap.get(grade, 0.0))

    # follower_loss (최적화 지표, 낮을수록 좋음)
    follower_loss = -ept_net + mdd * 2

    return {
        "crs_score":    crs,
        "grade":        grade,
        "copy_ratio":   round(copy_ratio, 4),
        "follower_loss": round(follower_loss, 6),
        "ept_net":      round(ept_net, 4),
        "flags":        flags,
        "breakdown": {
            "profit_factor": round(s_pf * 100, 2),
            "sharpe":        round(s_sharpe * 100, 2),
            "ept_net":       round(s_ept * 100, 2),
            "sortino":       round(s_sortino * 100, 2),
            "csr":           round(s_csr * 100, 2),
            "recovery":      round(s_rf * 100, 2),
            "mdd":           round(s_mdd * 100, 2),
            "ror":           round(s_ror * 100, 2),
            "mcl":           round(s_mcl * 100, 2),
            "purity":        round(s_purity * 100, 2),
            "sample":        round(s_sample * 100, 2),
        },
    }
