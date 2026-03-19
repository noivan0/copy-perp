#!/usr/bin/env python3
"""
scripts/mainnet_track.py — 메인넷 장기 PnL 추적 실행기

사용법:
  python3 scripts/mainnet_track.py              # 1회 수집 후 리포트
  python3 scripts/mainnet_track.py --loop       # 3시간마다 반복 수집
  python3 scripts/mainnet_track.py --report     # 누적 보고서만 출력
  python3 scripts/mainnet_track.py --days 7     # 최근 7일 보고서
"""

import os
import sys
import argparse
import time
import logging
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from core.mainnet_tracker import (
    collect_once,
    get_accumulated_report,
    DEFAULT_DB_PATH,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

LOOP_INTERVAL = 10800   # 3시간


# ── 리포트 포매터 ──────────────────────────────────────────────────
def format_report(report: dict) -> str:
    if "error" in report:
        return f"[오류] {report['error']}"

    SEP  = "=" * 68
    SEP2 = "-" * 68
    lines = [SEP]

    meta = report.get("meta", {})
    lines.append(f"  Copy Perp — 메인넷 장기 PnL 추적 리포트")
    lines.append(f"  수집 기간: {meta.get('first_date','?')} ~ {meta.get('latest_date','?')}")
    lines.append(f"  운영: {meta.get('duration_days', 0):.1f}일 | 수집 횟수: {meta.get('n_collections', 0)}회")
    lines.append(SEP)

    # 트레이더 실적
    lines.append("")
    lines.append("  [트레이더 실적 — 최신 기준]")
    lines.append(f"  {'트레이더':<12} {'등급':<4} {'CRS':<6} {'ROI_30d(첫)':<14} {'ROI_30d(최신)':<14} {'변화'}")
    lines.append(f"  {SEP2}")
    for t in report.get("trader_trend", []):
        lines.append(
            f"  {t['alias']:<12} {t['grade']:<4} {t['crs']:<6.1f} "
            f"{t['roi_30d_first']:>+10.2f}%    {t['roi_30d_latest']:>+10.2f}%    {t['roi_30d_delta']:>+.2f}%p"
        )

    # 팔로워 시뮬 PnL
    lines.append("")
    lines.append("  [팔로워 시뮬 PnL — 최신 수집 기준]")
    lines.append(f"  {'시나리오':<10} {'자본':>9} {'1일PnL':>10} {'7일PnL':>10} {'30일PnL':>10} {'30일ROI':>8}")
    lines.append(f"  {SEP2}")
    for sim in report.get("sim_pnl_latest", []):
        lines.append(
            f"  {sim['label']:<10} ${sim['capital']:>8,.0f} "
            f"  ${sim['pnl_1d']:>+7.2f}  ${sim['pnl_7d']:>+8.2f}  ${sim['pnl_30d']:>+8.2f}  {sim['roi_30d_pct']:>+6.2f}%"
        )

    # 신뢰도 기준
    trust = report.get("trust_latest", {})
    lines.append("")
    lines.append("  [신뢰도 기준 달성 현황]")
    l2 = "✅ 충족" if trust.get("l2_met") else "⚠️ 미충족"
    l3 = "✅ 충족" if trust.get("l3_target_met") else "⚠️ 미충족"
    lines.append(f"  L2 트레이더 기준: {l2}  (ROI≥10%: {trust.get('traders_roi30_ge10',0)}명, ROI≥30%: {trust.get('traders_roi30_ge30',0)}명)")
    lines.append(f"  L3 사용자 PnL:    {l3}  (안정형 30일 시뮬 ROI: {trust.get('sim_30d_roi_1k', 0):+.2f}%)")
    lines.append(f"  평균 CRS: {trust.get('avg_crs', 0):.1f} | 평균 ROI_30d: {trust.get('avg_roi_30d', 0):+.2f}%")

    # PnL 추이 (최근 5포인트)
    pnl_trend = report.get("pnl_trend", [])
    if len(pnl_trend) > 1:
        lines.append("")
        lines.append("  [안정형 $1k 30일 ROI 추이 (최근 5포인트)]")
        for pt in pnl_trend[-5:]:
            dt_str = datetime.fromtimestamp(pt['ts']).strftime("%Y-%m-%d %H:%M") if 'ts' in pt else pt.get('date','?')
            lines.append(f"  {dt_str}  {pt['roi_30d_pct']:>+.3f}%")

    lines.append(SEP)
    return "\n".join(lines)


def format_collect_result(result: dict) -> str:
    SEP = "=" * 68
    lines = [SEP, f"  1회 수집 완료 | {result['collected_date']} | {result['collected']}명", SEP]

    lines.append("")
    lines.append("  [트레이더 실적]")
    lines.append(f"  {'트레이더':<12} {'등급':<4} {'CRS':<6} {'ROI_1d':>8} {'ROI_7d':>8} {'ROI_30d':>9} {'PnL_30d':>12}")
    for s in result.get("snapshots", []):
        lines.append(
            f"  {s['alias']:<12} {s['grade']:<4} {s['crs']:<6.1f} "
            f"{s.get('roi1', s.get('roi_1d',0)):>+7.2f}% {s.get('roi7',s.get('roi_7d',0)):>+7.2f}% {s.get('roi30',s.get('roi_30d',0)):>+8.2f}%  ${s.get('pnl_30d',0):>10,.0f}"
        )

    lines.append("")
    lines.append("  [팔로워 시뮬 PnL]")
    lines.append(f"  {'시나리오':<10} {'자본':>9} {'1일':>10} {'7일':>10} {'30일':>10} {'30일ROI':>8}")
    for sim in result.get("sim_pnl", []):
        lines.append(
            f"  {sim['label']:<10} ${sim['capital']:>8,.0f} "
            f"  ${sim['pnl_1d']:>+7.2f}  ${sim['pnl_7d']:>+8.2f}  ${sim['pnl_30d']:>+8.2f}  {sim['roi_30d_pct']:>+6.2f}%"
        )

    trust = result.get("trust", {})
    lines.append("")
    l2 = "✅" if trust.get("l2_met") else "⚠️"
    l3 = "✅" if trust.get("l3_target_met") else "⚠️"
    lines.append(f"  신뢰도 — L2 트레이더: {l2}  L3 사용자PnL: {l3}")
    lines.append(f"  (ROI≥30% 트레이더: {trust.get('traders_roi30_ge30',0)}명 | 안정형30일: {trust.get('sim_30d_roi_1k',0):+.2f}%)")
    lines.append(SEP)
    return "\n".join(lines)


# ── 메인 ──────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Copy Perp 메인넷 장기 PnL 추적기")
    parser.add_argument("--loop",   action="store_true", help="3시간마다 반복 수집")
    parser.add_argument("--report", action="store_true", help="누적 보고서만 출력")
    parser.add_argument("--days",   type=int, default=30, help="보고서 기간 (일)")
    parser.add_argument("--db",     type=str, default=DEFAULT_DB_PATH, help="DB 경로")
    args = parser.parse_args()

    if args.report:
        report = get_accumulated_report(args.db, days=args.days)
        print(format_report(report))
        return

    if args.loop:
        logger.info(f"장기 추적 시작 (간격: {LOOP_INTERVAL//3600}시간 | DB: {args.db})")
        while True:
            try:
                result = collect_once(args.db)
                print(format_collect_result(result))
            except Exception as e:
                logger.error(f"수집 오류: {e}", exc_info=True)
            logger.info(f"다음 수집까지 {LOOP_INTERVAL//3600}시간 대기...")
            time.sleep(LOOP_INTERVAL)
    else:
        # 기본: 1회 수집 + 누적 보고서
        result = collect_once(args.db)
        print(format_collect_result(result))
        print()
        report = get_accumulated_report(args.db, days=args.days)
        print(format_report(report))


if __name__ == "__main__":
    main()
