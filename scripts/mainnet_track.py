#!/usr/bin/env python3
"""
scripts/mainnet_track.py — 메인넷 장기 PnL 추적 실행기

사용법:
  python3 scripts/mainnet_track.py              # 1회 수집 후 리포트 출력
  python3 scripts/mainnet_track.py --loop       # 3시간마다 반복 수집
  python3 scripts/mainnet_track.py --report     # 누적 데이터 리포트만
  python3 scripts/mainnet_track.py --days 7     # 7일치 누적 리포트
"""

import os
import sys
import argparse
import time
import json

# 프로젝트 루트를 path에 추가
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from core.mainnet_tracker import (
    collect_once,
    get_accumulated_report,
    DEFAULT_DB_PATH,
    TARGET_TRADERS,
)

LOOP_INTERVAL = 10800  # 3시간 (초)


# ── 리포트 포매터 ─────────────────────────────────────────────────
def format_report(report: dict) -> str:
    """누적 보고서 → 콘솔 출력 형식"""
    if "error" in report:
        return f"[오류] {report['error']}"

    lines = []
    meta = report.get("meta", {})
    trust_sum = report.get("trust_summary", {})

    lines.append("")
    lines.append("=" * 60)
    lines.append("  Copy Perp — 메인넷 장기 PnL 추적 리포트")
    lines.append(
        f"  수집 기간: {meta.get('first_date', 'N/A')} ~ {meta.get('last_date', 'N/A')} "
        f"({meta.get('duration_days', 0):.1f}일)"
    )
    lines.append(f"  데이터 포인트: {meta.get('data_points', 0)}회 수집")
    lines.append("=" * 60)

    # ── 트레이더 실적 추이 ────────────────────────────────────────
    lines.append("")
    lines.append("[트레이더 실적 추이]")
    header = f"{'트레이더':<12} {'등급':<4} {'CRS':>5}  {'ROI_30d(첫수집)':>15}  {'ROI_30d(최신)':>13}  {'변화':>8}  {'PnL_1d':>10}"
    lines.append(header)
    lines.append("-" * len(header))

    for t in report.get("trader_trends", []):
        first_roi = t.get("roi_30d_first", 0)
        latest_roi = t.get("roi_30d_latest", 0)
        change = t.get("roi_30d_change", 0)
        pnl_1d = t.get("pnl_1d_latest", 0)

        lines.append(
            f"{t['alias']:<12} {t['grade']:<4} {t['crs']:>5.1f}  "
            f"{first_roi:>+14.1f}%  "
            f"{latest_roi:>+12.1f}%  "
            f"{change:>+7.1f}%p  "
            f"${pnl_1d:>9,.0f}"
        )

    # ── 팔로워 시뮬 PnL ──────────────────────────────────────────
    lines.append("")
    lines.append("[팔로워 시뮬 PnL — 최신 기준]")
    sim_header = f"{'시나리오':<16} {'자본':>9}  {'1일':>9}  {'7일':>9}  {'30일(추정)':>11}  {'ROI':>8}  {'수익/전체'}"
    lines.append(sim_header)
    lines.append("-" * len(sim_header))

    SCENARIO_KO = {
        "conservative_1k":  "안정형",
        "balanced_5k":      "균형형",
        "aggressive_10k":   "공격형",
        "full_10k":         "풀포트",
    }
    sim = report.get("sim_pnl_latest", {})
    for key in ["conservative_1k", "balanced_5k", "aggressive_10k", "full_10k"]:
        s = sim.get(key, {})
        if not s:
            continue
        name_ko = SCENARIO_KO.get(key, key)
        capital = s.get("capital", 0)
        pnl_30d = s.get("pnl_cumulative", 0)
        roi = s.get("roi_pct", 0)
        win = s.get("win_traders", 0)
        total = s.get("total_traders", 0)

        # 1일/7일은 최신 스냅샷에서 추정 (30일 기준 비례)
        pnl_1d_est = pnl_30d / 30 if pnl_30d else 0
        pnl_7d_est = pnl_30d / 30 * 7 if pnl_30d else 0

        lines.append(
            f"{name_ko:<16} ${capital:>8,.0f}  "
            f"${pnl_1d_est:>+7.2f}  "
            f"${pnl_7d_est:>+7.2f}  "
            f"${pnl_30d:>+9.2f}  "
            f"{roi:>+7.2f}%  "
            f"{win}/{total}명"
        )

    # ── 신뢰도 기준 달성 현황 ─────────────────────────────────────
    trust = report.get("trust_latest", {})
    lines.append("")
    lines.append("[신뢰도 기준 달성 현황]")

    l2_met = trust.get("l2_met", 0)
    l3_met = trust.get("l3_target_met", 0)
    above_30 = trust.get("traders_above_roi30_30pct", 0)
    above_10 = trust.get("traders_above_roi30_10pct", 0)
    avg_crs = trust.get("avg_crs", 0)
    sim_30d_roi = trust.get("sim_30d_roi_1k", 0)
    sim_30d_full = trust.get("sim_30d_roi_10k_full", 0)
    total_traders = len(TARGET_TRADERS)

    lines.append(
        f"L2 트레이더 기준: {'✅ 충족' if l2_met else '❌ 미충족'} "
        f"(ROI≥30%: {above_30}/{total_traders}명 | ROI≥10%: {above_10}/{total_traders}명 | 평균CRS: {avg_crs:.1f})"
    )
    lines.append(
        f"L3 목표 기준:    {'✅ 충족' if l3_met else '❌ 미충족'} "
        f"(30일 시뮬 ROI — 안정형: {sim_30d_roi:+.2f}% | 풀포트: {sim_30d_full:+.2f}% | 목표: ≥7%)"
    )

    if trust_sum:
        lines.append(
            f"\n  [이력] L2달성: {trust_sum.get('l2_met_count')}/{trust_sum.get('total_checks')}회 "
            f"({trust_sum.get('l2_rate_pct', 0):.0f}%) | "
            f"L3달성: {trust_sum.get('l3_met_count')}/{trust_sum.get('total_checks')}회 "
            f"({trust_sum.get('l3_rate_pct', 0):.0f}%)"
        )

    lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)


def format_collect_result(result: dict) -> str:
    """1회 수집 결과 → 콘솔 출력"""
    lines = []
    lines.append("")
    lines.append("=" * 60)
    lines.append(f"  [수집 완료] {result.get('collected_date')} {time.strftime('%H:%M:%S')}")
    lines.append(f"  트레이더: {result.get('collected')}명 수집")
    lines.append("=" * 60)

    # 스냅샷 요약
    lines.append("")
    lines.append("[트레이더 실적]")
    snap_header = f"{'트레이더':<12} {'등급':<4} {'CRS':>5}  {'ROI_30d':>9}  {'ROI_7d':>8}  {'PnL_30d':>12}  {'Equity':>12}"
    lines.append(snap_header)
    lines.append("-" * len(snap_header))

    for s in result.get("snapshots", []):
        lines.append(
            f"{s.get('alias', ''):<12} {s.get('grade', ''):<4} {s.get('crs', 0):>5.1f}  "
            f"{s.get('roi_30d', 0):>+8.1f}%  "
            f"{s.get('roi_7d', 0):>+7.1f}%  "
            f"${s.get('pnl_30d', 0):>11,.0f}  "
            f"${s.get('equity', 0):>11,.0f}"
        )

    # 시뮬 요약
    lines.append("")
    lines.append("[팔로워 시뮬 PnL]")
    SCENARIO_KO = {
        "conservative_1k":  "안정형  $1,000",
        "balanced_5k":      "균형형  $5,000",
        "aggressive_10k":   "공격형 $10,000",
        "full_10k":         "풀포트 $10,000",
    }
    for key in ["conservative_1k", "balanced_5k", "aggressive_10k", "full_10k"]:
        s = result.get("sim_pnl", {}).get(key, {})
        if not s:
            continue
        name = SCENARIO_KO.get(key, key)
        pnl_1d = s.get("pnl_1d", 0)
        pnl_7d = s.get("pnl_7d", 0)
        pnl_30d = s.get("pnl_30d", 0)
        roi = s.get("roi_pct", 0)
        lines.append(
            f"  {name}  1d:${pnl_1d:>+7.2f}  7d:${pnl_7d:>+8.2f}  30d:${pnl_30d:>+9.2f}  ROI:{roi:>+6.2f}%"
        )

    # 신뢰도
    trust = result.get("trust", {})
    lines.append("")
    lines.append("[신뢰도 기준]")
    lines.append(
        f"  L2: {'✅ 충족' if trust.get('l2_met') else '❌ 미충족'} "
        f"(ROI≥30%: {trust.get('traders_above_roi30_30pct', 0)}/{len(TARGET_TRADERS)}명)"
    )
    lines.append(
        f"  L3: {'✅ 충족' if trust.get('l3_target_met') else '❌ 미충족'} "
        f"(30일 시뮬 ROI: {trust.get('sim_30d_roi_1k', 0):+.2f}% | 목표≥7%)"
    )
    lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)


# ── 메인 진입점 ──────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Copy Perp — 메인넷 장기 PnL 추적기")
    parser.add_argument("--loop", action="store_true", help="3시간마다 반복 수집")
    parser.add_argument("--report", action="store_true", help="누적 데이터 리포트만 출력")
    parser.add_argument("--days", type=int, default=30, help="리포트 기간 (일, 기본 30)")
    parser.add_argument("--db", type=str, default=DEFAULT_DB_PATH, help="DB 파일 경로")
    args = parser.parse_args()

    db_path = args.db

    # ── --report 모드 ────────────────────────────────────────────
    if args.report:
        print("[리포트 모드] 누적 데이터 조회 중...")
        report = get_accumulated_report(db_path=db_path, days=args.days)
        print(format_report(report))
        return

    # ── 1회 수집 ────────────────────────────────────────────────
    print(f"[메인넷 추적기] DB: {db_path}")
    result = collect_once(db_path=db_path)
    print(format_collect_result(result))

    # 수집 후 누적 리포트도 출력
    report = get_accumulated_report(db_path=db_path, days=args.days)
    print(format_report(report))

    # ── --loop 모드 ──────────────────────────────────────────────
    if args.loop:
        print(f"\n[루프 모드] {LOOP_INTERVAL // 3600}시간 간격으로 자동 수집...")
        while True:
            next_collect = time.time() + LOOP_INTERVAL
            next_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(next_collect))
            print(f"\n[대기] 다음 수집: {next_str}")

            try:
                time.sleep(LOOP_INTERVAL)
            except KeyboardInterrupt:
                print("\n[중단] 루프 종료")
                break

            try:
                result = collect_once(db_path=db_path)
                print(format_collect_result(result))
                report = get_accumulated_report(db_path=db_path, days=args.days)
                print(format_report(report))
            except Exception as e:
                print(f"[오류] 수집 실패: {e}")
                # 실패해도 루프 유지


if __name__ == "__main__":
    main()
