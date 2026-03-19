"""
누적 시계열 분석기
- results/mainnet_snapshots/ 의 모든 JSON을 읽어 시계열 PnL 분석
- 스냅샷 1개: 월간 추정치 계산
- 스냅샷 다수: 실제 delta 기반 정밀 계산
- 출력: results/accumulated_report_{YYYY-MM-DD}.json + 콘솔

Usage: python3 scripts/analyze_accumulated.py
"""

import json
import os
import math
import glob
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOTS_DIR = os.path.join(BASE_DIR, "results", "mainnet_snapshots")
REPORTS_DIR = os.path.join(BASE_DIR, "results")
DEEP_ANALYSIS_PATH = os.path.join(BASE_DIR, "trader_deep_analysis.json")

FOLLOWER_CAPITAL = 10_000
SLIPPAGE_HAIRCUT = 0.95

GRADE_COPY_RATIO = {"S": 0.10, "A": 0.07, "B": 0.04, "C": 0.00}


def calc_trust_score(trader: dict) -> float:
    roi_30d = trader.get("roi_30d", 0) or 0
    roi_7d = trader.get("roi_7d", 0) or 0
    sharpe = trader.get("sharpe_approx", 0) or 0
    equity = trader.get("equity", 1) or 1
    oi = trader.get("oi", 0) or 0

    roi_min, roi_max = 30.0, 90.0
    profitability = max(0, min(1, (roi_30d - roi_min) / (roi_max - roi_min))) * 35

    if roi_30d > 0:
        momentum_ratio = roi_7d / roi_30d
    else:
        momentum_ratio = 0
    momentum = max(0, min(1, momentum_ratio / 1.5)) * 25

    if sharpe > 0:
        sharpe_log = math.log10(max(0.1, sharpe))
        sharpe_norm = max(0, min(1, (sharpe_log + 1) / 2))
    else:
        sharpe_norm = 0
    stability = sharpe_norm * 25

    if equity > 0 and oi > 0:
        lev_ratio = oi / equity
        lev_score = max(0, min(1, 1 / (1 + lev_ratio))) * 15
    else:
        lev_score = 15

    return round(profitability + momentum + stability + lev_score, 2)


def get_grade(trust_score: float) -> str:
    if trust_score >= 70:
        return "S"
    elif trust_score >= 55:
        return "A"
    elif trust_score >= 40:
        return "B"
    else:
        return "C"


def load_all_snapshots() -> list[dict]:
    """모든 스냅샷 파일 시간순 로드"""
    files = sorted(glob.glob(os.path.join(SNAPSHOTS_DIR, "*.json")))
    snapshots = []
    for f in files:
        with open(f, "r") as fp:
            snapshots.append(json.load(fp))
    return snapshots


def load_local_metadata() -> dict:
    """로컬 trader_deep_analysis.json에서 보조 데이터 로드"""
    with open(DEEP_ANALYSIS_PATH, "r") as f:
        data = json.load(f)
    return {t["address"]: t for t in data.get("ranked_traders", []) if t.get("tier") == 1}


def analyze_single_snapshot(snapshot: dict, local_meta: dict) -> dict:
    """스냅샷 1개 기반 월간 추정 분석"""
    traders = snapshot["traders"]
    snap_time = snapshot["snapshot_at"]

    results = []
    for t in traders:
        addr = t["address"]
        # sharpe/oi 보완
        if addr in local_meta:
            meta = local_meta[addr]
            if not t.get("sharpe_approx"):
                t["sharpe_approx"] = meta.get("sharpe_approx", 0)
            if not t.get("oi"):
                t["oi"] = meta.get("oi", 0)

        trust = calc_trust_score(t)
        grade = get_grade(trust)
        copy_ratio = GRADE_COPY_RATIO[grade]
        roi_30d = t.get("roi_30d", 0) or 0
        effective_roi = roi_30d * SLIPPAGE_HAIRCUT / 100
        allocated = FOLLOWER_CAPITAL * copy_ratio
        pnl_30d = round(allocated * effective_roi, 2)

        results.append({
            "address": addr,
            "roi_30d": roi_30d,
            "trust_score": trust,
            "grade": grade,
            "copy_ratio": copy_ratio,
            "estimated_pnl_30d": pnl_30d,
            "snapshot_at": snap_time,
            "calc_method": "single_snapshot_estimate",
        })

    return {
        "method": "single_snapshot_estimate",
        "snapshot_at": snap_time,
        "traders": results,
    }


def analyze_multi_snapshot(snapshots: list[dict], local_meta: dict) -> dict:
    """다중 스냅샷 기반 delta 정밀 분석"""
    # 주소별 시계열 구성
    timeseries = {}  # address → [(time, pnl_30d, roi_30d), ...]

    for snap in snapshots:
        snap_time = snap["snapshot_at"]
        for t in snap["traders"]:
            addr = t["address"]
            if addr not in timeseries:
                timeseries[addr] = []
            timeseries[addr].append({
                "time": snap_time,
                "pnl_30d": t.get("pnl_30d", 0) or 0,
                "roi_30d": t.get("roi_30d", 0) or 0,
                "equity": t.get("equity", 0) or 0,
            })

    # 최신 스냅샷 기준으로 신뢰도 점수 계산
    latest_snap = snapshots[-1]
    latest_traders = {t["address"]: t for t in latest_snap["traders"]}

    results = []
    for addr, series in timeseries.items():
        t = latest_traders.get(addr, {})
        # 보조 데이터 보완
        if addr in local_meta:
            meta = local_meta[addr]
            if not t.get("sharpe_approx"):
                t["sharpe_approx"] = meta.get("sharpe_approx", 0)
            if not t.get("oi"):
                t["oi"] = meta.get("oi", 0)

        trust = calc_trust_score(t) if t else 0
        grade = get_grade(trust)
        copy_ratio = GRADE_COPY_RATIO[grade]

        # delta 기반 누적 PnL 계산
        pnl_deltas = []
        for i in range(1, len(series)):
            delta = series[i]["pnl_30d"] - series[i-1]["pnl_30d"]
            pnl_deltas.append(delta)

        if pnl_deltas:
            # 실제 delta 기반: 각 기간 delta에 copy_ratio * slippage 적용
            accumulated_follower_pnl = sum(
                (FOLLOWER_CAPITAL * copy_ratio) * (d / max(series[i]["equity"], 1)) * SLIPPAGE_HAIRCUT
                for i, d in enumerate(pnl_deltas)
            )
        else:
            # delta 없으면 추정치
            latest_roi = series[-1]["roi_30d"] if series else 0
            effective_roi = latest_roi * SLIPPAGE_HAIRCUT / 100
            accumulated_follower_pnl = FOLLOWER_CAPITAL * copy_ratio * effective_roi

        results.append({
            "address": addr,
            "snapshot_count": len(series),
            "roi_30d_latest": series[-1]["roi_30d"] if series else 0,
            "roi_30d_first": series[0]["roi_30d"] if series else 0,
            "trust_score": trust,
            "grade": grade,
            "copy_ratio": copy_ratio,
            "accumulated_follower_pnl": round(accumulated_follower_pnl, 2),
            "calc_method": "multi_snapshot_delta",
            "time_range": f"{series[0]['time'][:16]} ~ {series[-1]['time'][:16]}",
        })

    return {
        "method": "multi_snapshot_delta",
        "snapshot_count": len(snapshots),
        "time_range": f"{snapshots[0]['snapshot_at'][:16]} ~ {snapshots[-1]['snapshot_at'][:16]}",
        "traders": results,
    }


def run_analysis():
    """누적 분석 실행"""
    os.makedirs(REPORTS_DIR, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    report_path = os.path.join(REPORTS_DIR, f"accumulated_report_{today}.json")

    snapshots = load_all_snapshots()
    local_meta = load_local_metadata()

    if not snapshots:
        print("⚠️  스냅샷 없음. mainnet_tracker.py를 먼저 실행하세요.")
        return None

    print(f"\n{'='*60}")
    print(f"=== CopyPerp 누적 분석 리포트 ===")
    print(f"{'='*60}")
    print(f"스냅샷 수: {len(snapshots)}개")

    if len(snapshots) == 1:
        print(f"분석 방법: 단일 스냅샷 기반 월간 추정")
        analysis = analyze_single_snapshot(snapshots[0], local_meta)
        field_name = "estimated_pnl_30d"
    else:
        print(f"분석 방법: 다중 스냅샷 delta 기반 정밀 계산")
        analysis = analyze_multi_snapshot(snapshots, local_meta)
        field_name = "accumulated_follower_pnl"

    # 등급별 집계
    traders = analysis["traders"]
    grade_groups = {"S": [], "A": [], "B": [], "C": []}
    for t in traders:
        grade_groups[t["grade"]].append(t)

    print(f"\n[등급별 팔로워 PnL]")
    print(f"{'등급':<5} {'트레이더수':>8} {'평균PnL':>10} {'합계PnL':>12}")
    print("-" * 40)
    total_all = 0
    for grade in ["S", "A", "B", "C"]:
        group = grade_groups[grade]
        if group:
            pnls = [t[field_name] for t in group]
            avg_pnl = sum(pnls) / len(pnls)
            sum_pnl = sum(pnls)
            total_all += sum_pnl
            print(f"{grade:<5} {len(group):>8}명 {avg_pnl:>+9.0f}$ {sum_pnl:>+11.0f}$")

    print(f"\n[전체 포트폴리오 PnL 합계]")
    print(f"  S+A+B 전체: +${total_all:.0f} ({total_all/FOLLOWER_CAPITAL*100:.1f}%)")

    s_a_pnl = sum(t[field_name] for t in grade_groups["S"] + grade_groups["A"])
    print(f"  S+A만:      +${s_a_pnl:.0f} ({s_a_pnl/FOLLOWER_CAPITAL*100:.1f}%)")

    # 리포트 저장
    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "snapshot_count": len(snapshots),
        "follower_capital": FOLLOWER_CAPITAL,
        "slippage_haircut": SLIPPAGE_HAIRCUT,
        "grade_summary": {
            grade: {
                "count": len(group),
                "total_follower_pnl": sum(t[field_name] for t in group),
                "avg_follower_pnl": sum(t[field_name] for t in group) / len(group) if group else 0,
            }
            for grade, group in grade_groups.items()
        },
        "analysis": analysis,
    }

    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"\n✅ 리포트 저장: {report_path}")
    print(f"{'='*60}")

    return report


if __name__ == "__main__":
    run_analysis()
