"""
팔로워 가상 포트폴리오 PnL 시뮬레이터
- 팔로워 자본 $10,000 기준
- 신뢰도 4축 점수 → 등급 → copy_ratio 결정
- 슬리피지 5% 헤어컷 적용

Usage: python3 scripts/follower_pnl_simulator.py
"""

import json
import os
import math
import glob
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEEP_ANALYSIS_PATH = os.path.join(BASE_DIR, "trader_deep_analysis.json")
SNAPSHOTS_DIR = os.path.join(BASE_DIR, "results", "mainnet_snapshots")

FOLLOWER_CAPITAL = 10_000  # $10,000
SLIPPAGE_HAIRCUT = 0.95    # 트레이더 ROI * 0.95

# 등급별 copy_ratio
GRADE_COPY_RATIO = {
    "S": 0.10,
    "A": 0.07,
    "B": 0.04,
    "C": 0.00,
}


def calc_trust_score(trader: dict) -> float:
    """
    신뢰도 점수 계산 (100점 만점)
    - 수익성 35점: roi_30d (30~90% 정규화)
    - 모멘텀 25점: roi_7d / roi_30d 비율
    - 안정성 25점: sharpe_approx log10 정규화
    - 레버리지 안전 15점: OI/Equity 비율 역수
    """
    roi_30d = trader.get("roi_30d", 0) or 0
    roi_7d = trader.get("roi_7d", 0) or 0
    sharpe = trader.get("sharpe_approx", 0) or 0
    equity = trader.get("equity", 1) or 1
    oi = trader.get("oi", 0) or 0

    # 1. 수익성 (35점): roi_30d 30~90% 범위 정규화
    roi_min, roi_max = 30.0, 90.0
    profitability = max(0, min(1, (roi_30d - roi_min) / (roi_max - roi_min))) * 35

    # 2. 모멘텀 (25점): roi_7d / roi_30d 비율 (0~1.5 → 0~25점)
    if roi_30d > 0:
        momentum_ratio = roi_7d / roi_30d
    else:
        momentum_ratio = 0
    momentum = max(0, min(1, momentum_ratio / 1.5)) * 25

    # 3. 안정성 (25점): sharpe_approx log10 정규화 (0.1~10 범위)
    if sharpe > 0:
        sharpe_log = math.log10(max(0.1, sharpe))  # log10(0.1)=-1, log10(10)=1
        sharpe_norm = max(0, min(1, (sharpe_log + 1) / 2))  # -1~1 → 0~1
    else:
        sharpe_norm = 0
    stability = sharpe_norm * 25

    # 4. 레버리지 안전 (15점): OI/Equity 역수 (낮을수록 안전)
    if equity > 0 and oi > 0:
        lev_ratio = oi / equity  # 0 = 무포지션, 1 = 100% 레버, 2+ = 고위험
        lev_score = max(0, min(1, 1 / (1 + lev_ratio))) * 15
    else:
        lev_score = 15  # 포지션 없으면 최고점

    total = profitability + momentum + stability + lev_score
    return round(total, 2)


def get_grade(trust_score: float) -> str:
    if trust_score >= 70:
        return "S"
    elif trust_score >= 55:
        return "A"
    elif trust_score >= 40:
        return "B"
    else:
        return "C"


def calc_follower_pnl_30d(trader: dict, trust_score: float) -> float:
    """팔로워 30일 PnL 계산 ($10,000 자본 기준)"""
    grade = get_grade(trust_score)
    copy_ratio = GRADE_COPY_RATIO[grade]
    roi_30d = trader.get("roi_30d", 0) or 0
    effective_roi = roi_30d * SLIPPAGE_HAIRCUT / 100  # % → 소수
    allocated = FOLLOWER_CAPITAL * copy_ratio
    pnl = allocated * effective_roi
    return round(pnl, 2)


def load_latest_snapshot() -> dict | None:
    """최신 스냅샷 파일 로드"""
    files = sorted(glob.glob(os.path.join(SNAPSHOTS_DIR, "*.json")))
    if not files:
        return None
    with open(files[-1], "r") as f:
        return json.load(f)


def load_local_traders() -> list[dict]:
    """trader_deep_analysis.json에서 Tier1 트레이더 로드"""
    with open(DEEP_ANALYSIS_PATH, "r") as f:
        data = json.load(f)
    return [t for t in data.get("ranked_traders", []) if t.get("tier") == 1]


def run_simulation():
    """팔로워 PnL 시뮬레이션 실행"""
    # 데이터 소스 결정: 스냅샷 우선, 없으면 로컬
    snapshot = load_latest_snapshot()
    if snapshot:
        traders = snapshot["traders"]
        snap_time = snapshot["snapshot_at"]
        data_source = "mainnet_snapshot"
        # 스냅샷에 sharpe/oi 없으면 로컬에서 보완
        local_traders = {t["address"]: t for t in load_local_traders()}
        for t in traders:
            addr = t["address"]
            if addr in local_traders:
                if not t.get("sharpe_approx"):
                    t["sharpe_approx"] = local_traders[addr].get("sharpe_approx", 0)
                if not t.get("oi"):
                    t["oi"] = local_traders[addr].get("oi", 0)
    else:
        traders = load_local_traders()
        snap_time = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        data_source = "local_fallback"

    # 각 트레이더 분석
    results = []
    for t in traders:
        trust = calc_trust_score(t)
        grade = get_grade(trust)
        copy_ratio = GRADE_COPY_RATIO[grade]
        pnl_30d = calc_follower_pnl_30d(t, trust)
        results.append({
            "address": t["address"],
            "roi_30d": t.get("roi_30d", 0) or 0,
            "trust_score": trust,
            "grade": grade,
            "copy_ratio": copy_ratio,
            "follower_pnl_30d": pnl_30d,
        })

    # 등급별 정렬
    grade_order = {"S": 0, "A": 1, "B": 2, "C": 3}
    results.sort(key=lambda x: (grade_order[x["grade"]], -x["trust_score"]))

    # 출력
    snap_display = snap_time[:16].replace("T", " ")
    print(f"\n{'='*60}")
    print(f"=== CopyPerp 실적 추적 리포트 ===")
    print(f"{'='*60}")
    print(f"스냅샷: {snap_display} | 트레이더 {len(results)}명")
    print(f"데이터: {data_source}")
    print()

    print("[트레이더별 실적]")
    print(f"{'등급':<5} {'트레이더':<16} {'ROI30d':>7} {'신뢰도':>7} {'CopyR':>6} {'팔로워PnL(30d)':>14}")
    print("-" * 60)
    for r in results:
        addr_short = r["address"][:12]
        pnl_str = f"+${r['follower_pnl_30d']:.0f}" if r["follower_pnl_30d"] >= 0 else f"-${abs(r['follower_pnl_30d']):.0f}"
        copy_pct = f"{int(r['copy_ratio']*100)}%"
        print(f"{r['grade']:<5} {addr_short:<16} {r['roi_30d']:>6.1f}% {r['trust_score']:>7.1f} {copy_pct:>6} {pnl_str:>14}")

    print()

    # 스냅샷 현황
    snap_files = sorted(glob.glob(os.path.join(SNAPSHOTS_DIR, "*.json")))
    snap_count = len(snap_files)
    first_snap = "N/A"
    if snap_files:
        first_data = json.load(open(snap_files[0]))
        first_snap = first_data.get("snapshot_at", "N/A")[:16].replace("T", " ")

    print("[누적 데이터 현황]")
    print(f"스냅샷 수: {snap_count}개 ({first_snap} ~)")
    print(f"다음 스냅샷까지: 1시간")
    print()

    # 포트폴리오 시나리오
    s_grade = [r for r in results if r["grade"] == "S"]
    a_grade = [r for r in results if r["grade"] == "A"]
    b_grade = [r for r in results if r["grade"] == "B"]
    c_grade = [r for r in results if r["grade"] == "C"]

    # 보수적: S등급 1명 (최고)
    conservative_pnl = s_grade[0]["follower_pnl_30d"] if s_grade else 0
    conservative_roi = conservative_pnl / FOLLOWER_CAPITAL * 100

    # 균형: S+A 상위 3명
    top3 = (s_grade + a_grade)[:3]
    balanced_pnl = sum(r["follower_pnl_30d"] for r in top3)
    balanced_roi = balanced_pnl / FOLLOWER_CAPITAL * 100

    # 적극적: S+A 전체
    all_sa = s_grade + a_grade
    aggressive_pnl = sum(r["follower_pnl_30d"] for r in all_sa)
    aggressive_roi = aggressive_pnl / FOLLOWER_CAPITAL * 100

    print("[포트폴리오 시나리오]")
    print(f"- 보수적 (S등급 1명):       월 +${conservative_pnl:.0f} / +{conservative_roi:.1f}%")
    print(f"- 균형   (S+A 상위 3명):    월 +${balanced_pnl:.0f} / +{balanced_roi:.1f}%")
    print(f"- 적극적 (S+A 전체):        월 +${aggressive_pnl:.0f} / +{aggressive_roi:.1f}%")
    print()

    print("[신뢰도 등급 분포]")
    print(f"S(70+): {len(s_grade)}명 | A(55-69): {len(a_grade)}명 | B(40-54): {len(b_grade)}명 | C(<40): {len(c_grade)}명")
    print(f"{'='*60}")

    return results


if __name__ == "__main__":
    results = run_simulation()
