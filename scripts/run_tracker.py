#!/usr/bin/env python3
"""
scripts/run_tracker.py — Mainnet 장기 추적 실행기

모드:
  --once    : 지금 바로 스냅샷 1회 수집 + 리포트
  --seed    : 기존 파일에서 과거 데이터 시드
  --report  : 현재 누적 데이터 리포트만 출력
  --daemon  : 매일 00:10 UTC 자동 수집 (supervisord용)

사용:
  python3 scripts/run_tracker.py --once
  python3 scripts/run_tracker.py --seed
  python3 scripts/run_tracker.py --report
"""

import asyncio
import argparse
import json
import os
import sys
import time
import math
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db.database import init_db
from db.pnl_tracker import apply_migrations as apply_pnl_migrations
from core.performance import apply_perf_migrations
from core.mainnet_tracker import (
    apply_tracker_migrations,
    collect_snapshot,
    compute_all_long_stats,
    get_trust_evolution,
    get_long_term_report,
    seed_historical_data,
    TRACKED_TRADERS,
    COPY_RATIO, REALISM_FACTOR,
)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "copy_perp.db")

C = {
    "g": "\033[92m", "r": "\033[91m", "y": "\033[93m",
    "c": "\033[96m", "b": "\033[1m",  "d": "\033[2m",
    "x": "\033[0m",  "m": "\033[95m",
}
def c(col, txt): return f"{C[col]}{txt}{C['x']}"
def bar(v, mx=100, w=14): return "█"*int(w*min(v,mx)/mx) + "░"*(w-int(w*min(v,mx)/mx))


async def setup_db():
    conn = await init_db(DB_PATH)
    await apply_pnl_migrations(conn)
    await apply_perf_migrations(conn)
    await apply_tracker_migrations(conn)
    return conn


def print_trust_evolution(ev: dict):
    ts = ev["tracking_summary"]
    days = ts["total_tracking_days"]

    print(f"\n{c('b','━'*64)}")
    print(c("b", c("c", "  📡 Mainnet 장기 추적 현황")))
    print(c("b", "━"*64))

    day_color = "g" if days >= 30 else "y" if days >= 7 else "r"
    print(f"""
  추적 기간:  {c(day_color, c('b', f'{days}일 누적'))}
  기간:       {c('d', f'{ts["first_date"]} ~ {ts["last_date"]}')}
  트레이더:   {ts["traders_tracked"]}명 모니터링
  상태:       {ts["message"]}
""")

    # 기간별 통계
    ps = ev.get("period_stats", {})
    if ps:
        print(c("b", "  기간별 통계 (데이터 누적 기반)"))
        print(c("d", "  " + "─"*56))
        print(f"  {'기간':>6}  {'평균ROI':>8}  {'Sharpe':>7}  {'WinRate':>8}  {'MaxDD':>7}  {'신뢰도'}")
        print(c("d", "  " + "─"*56))
        for period, stats in sorted(ps.items()):
            roi = stats["avg_roi_pct"]
            sh  = stats["avg_sharpe"]
            wr  = stats["avg_win_rate"]
            dd  = stats["avg_max_dd"]
            conf = stats["min_confidence"]
            conf_icon = {"HIGH":"🔒","MEDIUM":"✅","LOW":"⚠️","INSUFFICIENT":"❌"}.get(conf, "?")
            roi_color = "g" if roi > 0 else "r"
            sign = "+" if roi >= 0 else ""
            print(f"  {period:>6}  {c(roi_color, f'{sign}{roi:.2f}%'):>18}  "
                  f"{c('c', f'{sh:.2f}'):>17}  "
                  f"{c('g' if wr>50 else 'y', f'{wr:.1f}%'):>18}  "
                  f"{c('y', f'{dd:.1f}%'):>17}  {conf_icon}{conf}")

    # 팔로워 수익
    fr = ev.get("follower_returns", {})
    if fr:
        print()
        print(c("b", "  팔로워 복사 수익 누적 ($10,000 · 10% 복사)"))
        print(c("d", "  " + "─"*56))
        for key, data in fr.items():
            grade = key.replace("grade_", "")
            d = data["tracking_days"]
            pnl = data["total_net_pnl"]
            peak = data["peak_cumulative"]
            best = data["best_day_pnl"]
            worst = data["worst_day_pnl"]
            pnl_color = "g" if pnl >= 0 else "r"
            sign = "+" if pnl >= 0 else ""
            label = "🏆 S등급만" if grade == "S" else "⭐ A등급+"
            print(f"\n  {c('b', label)}  ({d}일 추적)")
            print(f"    누적 순수익: {c(pnl_color, c('b', f'{sign}${pnl:,.2f}'))}")
            print(f"    최대 누적:   {c('c', f'${peak:,.2f}')}")
            print(f"    최고 하루:   {c('g', f'+${best:,.2f}')}")
            print(f"    최저 하루:   {c('r', f'${worst:,.2f}')}")


def print_long_term_report(report: dict):
    pd = report["period_days"]
    pf = report["portfolio"]

    print(f"\n{c('b','━'*64)}")
    print(c("b", c("c", f"  📊 장기 실적 리포트 ({pd}일)")))
    print(c("b", "━"*64))

    vt = pf["verified_traders"]
    fn = pf["follower_net_pnl"]
    td = pf["tracking_days"]
    fn_color = "g" if fn >= 0 else "r"
    fn_sign  = "+" if fn >= 0 else ""

    print(f"""
  검증 트레이더:  {c('c', f'{vt}명')} (데이터 5일+ 충족)
  추적 일수:      {c('c', f'{td}일')}
  팔로워 순수익:  {c(fn_color, c('b', f'{fn_sign}${fn:,.2f} USDC'))}
  팔로워 누적고점: {c('c', f'${pf["follower_peak_cum"]:,.2f} USDC')}
""")

    traders = report["traders"]
    if not traders:
        print(c("d", "  아직 충분한 데이터가 없습니다. 스냅샷을 더 수집해주세요."))
        return

    print(f"  {'트레이더':<12} {'등급':>5} {'포인트':>6} {'ROI':>9} {'Sharpe':>7} {'WinRate':>8} {'MaxDD':>7} {'신뢰도'}")
    print(c("d", "  " + "─"*68))

    for t in traders:
        grade = t.get("grade", "?")
        dp    = int(t.get("data_points", 0))
        if dp == 0:
            continue
        roi   = float(t.get("total_roi_pct", 0))
        sh    = float(t.get("sharpe_ratio", 0))
        wr    = float(t.get("win_rate_pct", 0))
        dd    = float(t.get("max_dd_pct", 0))
        conf  = t.get("confidence", "?")

        grade_color = "g" if grade in ("S","A") else "y" if grade == "B" else "d"
        roi_color   = "g" if roi >= 0 else "r"
        conf_icon   = {"HIGH":"🔒","MEDIUM":"✅","LOW":"⚠️","INSUFFICIENT":"❌"}.get(conf,"?")
        sign = "+" if roi >= 0 else ""

        print(
            f"  {c('c', t.get('alias','?')):<12} "
            f"{c(grade_color, grade):>13} "
            f"{c('d', f'{dp}d'):>15} "
            f"{c(roi_color, f'{sign}{roi:.2f}%'):>19} "
            f"{c('c', f'{sh:.2f}'):>17} "
            f"{c('g' if wr>50 else 'y', f'{wr:.1f}%'):>18} "
            f"{c('y', f'{dd:.1f}%'):>17}  {conf_icon}"
        )


async def run_once(conn):
    print(c("b", "\n🔄 Mainnet 스냅샷 수집 중..."))
    result = await collect_snapshot(conn)
    print(f"  ✓ {result['saved']}명 저장 | 날짜: {result['date']}")
    if result["errors"]:
        for e in result["errors"]:
            print(c("y", f"  ⚠ {e}"))

    print(c("b", "\n📊 장기 통계 계산 중..."))
    stats = await compute_all_long_stats(conn)
    computed = [s for s in stats if s.get("data_points", 0) > 0]
    print(f"  ✓ {len(computed)}건 계산 완료")

    return result


async def run_report(conn):
    ev = await get_trust_evolution(conn)
    print_trust_evolution(ev)

    for pd in (7, 30):
        report = await get_long_term_report(conn, pd)
        print_long_term_report(report)

    # JSON 저장
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trust_evolution": ev,
        "report_30d": await get_long_term_report(conn, 30),
    }
    out_path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                             "demo", "longterm_report.json")
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(c("g", f"\n  ✓ JSON 저장: {out_path}"))


async def main():
    parser = argparse.ArgumentParser(description="CopyPerp Mainnet Tracker")
    parser.add_argument("--once",   action="store_true", help="스냅샷 1회 수집 + 리포트")
    parser.add_argument("--seed",   action="store_true", help="기존 파일에서 과거 데이터 시드")
    parser.add_argument("--report", action="store_true", help="현재 누적 리포트 출력")
    parser.add_argument("--daemon", action="store_true", help="매일 자동 수집 (상시 실행)")
    args = parser.parse_args()

    conn = await setup_db()

    if args.seed:
        print(c("b", "🌱 과거 데이터 시드 중..."))
        n = await seed_historical_data(conn)
        print(f"  ✓ {n}건 시드 완료")
        await compute_all_long_stats(conn)
        await run_report(conn)

    elif args.once:
        await run_once(conn)
        await run_report(conn)

    elif args.report:
        await run_report(conn)

    elif args.daemon:
        print(c("b", "🤖 Daemon 모드 시작 — 매일 00:10 UTC 수집"))
        while True:
            now = datetime.now(timezone.utc)
            # 다음 00:10 UTC 계산
            target = now.replace(hour=0, minute=10, second=0, microsecond=0)
            if now >= target:
                from datetime import timedelta
                target += timedelta(days=1)
            wait_sec = (target - now).total_seconds()
            print(f"  다음 수집: {target.strftime('%Y-%m-%d %H:%M UTC')} (약 {wait_sec/3600:.1f}시간 후)")
            await asyncio.sleep(wait_sec)

            print(c("b", f"\n[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}] 자동 수집"))
            try:
                await run_once(conn)
            except Exception as e:
                print(c("r", f"  오류: {e}"))

    else:
        # 기본: 시드 + 1회 수집 + 리포트
        print(c("b", "🚀 CopyPerp Mainnet Tracker"))
        print(c("d", "  시드 → 수집 → 리포트 순서로 실행합니다\n"))

        n = await seed_historical_data(conn)
        print(f"  ✓ 시드 {n}건")

        await run_once(conn)
        await run_report(conn)

    await conn.close()
    print(c("b", "\n" + "="*64))
    print(c("c", c("b", "  CopyPerp — 데이터가 쌓일수록 신뢰가 쌓인다")))
    print(c("b", "="*64 + "\n"))


if __name__ == "__main__":
    asyncio.run(main())
