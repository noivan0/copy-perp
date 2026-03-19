"""
Copy Perp — 장기 누적 Papertrading 실행기
- 세션을 반복 실행하며 누적 성과를 JSON으로 기록
- 각 세션 종료 후 summary 자동 업데이트
- CARP 필터 적용: 승률 4% 이하 트레이더 자동 제외
"""
import json
import os
import time
import subprocess
import sys

WORK_DIR = os.path.dirname(os.path.abspath(__file__))
SUMMARY_PATH = os.path.join(WORK_DIR, "longterm_summary.json")
SESSION_DIR = os.path.join(WORK_DIR, "sessions")
os.makedirs(SESSION_DIR, exist_ok=True)

SESSION_DURATION_MIN = 120    # 2시간 단위 세션
POLL_INTERVAL_SEC = 120       # 2분 폴링
DEFAULT_STRATEGY  = "balanced"  # 기본 전략: balanced (Mainnet 최적화)


def load_summary() -> dict:
    if os.path.exists(SUMMARY_PATH):
        with open(SUMMARY_PATH) as f:
            return json.load(f)
    return {
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_duration_min": 0,
        "sessions": 0,
        "initial_capital": 10000.0,
        "cumulative_pnl": 0.0,
        "cumulative_roi_pct": 0.0,
        "total_trades": 0,
        "total_wins": 0,
        "total_losses": 0,
        "best_session_pnl": 0.0,
        "worst_session_pnl": 0.0,
        "peak_capital": 10000.0,
        "current_capital": 10000.0,
        "max_dd_pct": 0.0,
        "session_log": [],
    }


def update_summary(summary: dict, result: dict) -> dict:
    pnl = result.get("total_pnl", 0)
    wins = result.get("wins", 0)
    losses = result.get("losses", 0)
    trades = result.get("total_trades", 0)
    duration = result.get("duration_min", 0)
    dd = result.get("max_dd_pct", 0)

    summary["sessions"] += 1
    summary["total_duration_min"] += duration
    summary["cumulative_pnl"] += pnl
    summary["current_capital"] += pnl
    summary["cumulative_roi_pct"] = (
        summary["cumulative_pnl"] / summary["initial_capital"] * 100
    )
    summary["total_trades"] += trades
    summary["total_wins"] += wins
    summary["total_losses"] += losses

    if summary["current_capital"] > summary["peak_capital"]:
        summary["peak_capital"] = summary["current_capital"]

    cur_dd = (summary["peak_capital"] - summary["current_capital"]) / summary["peak_capital"] * 100
    if cur_dd > summary["max_dd_pct"]:
        summary["max_dd_pct"] = cur_dd

    if pnl > summary["best_session_pnl"]:
        summary["best_session_pnl"] = pnl
    if pnl < summary["worst_session_pnl"]:
        summary["worst_session_pnl"] = pnl

    wt = summary["total_wins"] + summary["total_losses"]
    win_rate = summary["total_wins"] / wt if wt else 0

    session_entry = {
        "session": summary["sessions"],
        "time": time.strftime("%Y-%m-%d %H:%M"),
        "duration_min": round(duration, 1),
        "pnl": round(pnl, 4),
        "roi_pct": round(result.get("roi_pct", 0), 4),
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 4),
        "profit_factor": result.get("profit_factor", 0),
        "max_dd_pct": round(dd, 4),
        "capital_after": round(summary["current_capital"], 2),
    }
    summary["session_log"].append(session_entry)
    summary["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    with open(SUMMARY_PATH, "w") as f:
        json.dump(summary, f, indent=2)

    return summary


def print_summary(summary: dict):
    s = summary
    hrs = s["total_duration_min"] / 60
    print("\n" + "=" * 65)
    print("  📊 Copy Perp 장기 누적 실적 리포트")
    print("=" * 65)
    print(f"  시작일:         {s['started_at']}")
    print(f"  누적 실행:      {s['total_duration_min']:.0f}분 ({hrs:.1f}시간) / {s['sessions']}세션")
    print(f"  초기 자본:      ${s['initial_capital']:,.2f}")
    print(f"  현재 자산:      ${s['current_capital']:,.2f}")
    print(f"  누적 PnL:       ${s['cumulative_pnl']:+,.2f} ({s['cumulative_roi_pct']:+.2f}%)")
    print(f"  최고 자산:      ${s['peak_capital']:,.2f}")
    print(f"  누적 MDD:       {s['max_dd_pct']:.2f}%")
    print()
    print(f"  총 거래:        {s['total_trades']}건")
    wt = s["total_wins"] + s["total_losses"]
    wr = s["total_wins"] / wt if wt else 0
    print(f"  승/패:          {s['total_wins']}W / {s['total_losses']}L ({wr:.1%})")
    print(f"  최고 세션 PnL:  ${s['best_session_pnl']:+,.2f}")
    print(f"  최저 세션 PnL:  ${s['worst_session_pnl']:+,.2f}")
    print()
    print("  ── 세션 로그 ─────────────────────────────────────")
    print(f"  {'#':>3} {'시간':>14} {'시간(분)':>7} {'PnL':>9} {'ROI%':>7} {'거래':>5} {'WR':>6} {'자산':>10}")
    for e in s["session_log"][-20:]:  # 최근 20세션
        print(f"  {e['session']:>3} {e['time']:>14} {e['duration_min']:>7.0f} "
              f"${e['pnl']:>+8.2f} {e['roi_pct']:>+6.2f}% {e['trades']:>5} "
              f"{e['win_rate']:>5.1%} ${e['capital_after']:>9,.2f}")
    print("=" * 65)


def run_session(session_num: int, strategy: str = DEFAULT_STRATEGY) -> dict:
    ts = time.strftime("%Y%m%d_%H%M%S")
    out = os.path.join(SESSION_DIR, f"session_{session_num:04d}_{ts}.json")
    script = os.path.join(WORK_DIR, "run_papertrading.py")

    print(f"\n{'='*65}")
    print(f"  🚀 세션 #{session_num} 시작 | {SESSION_DURATION_MIN}분 | 전략: {strategy}")
    print(f"  출력: {os.path.basename(out)}")
    print(f"{'='*65}")

    cmd = [
        sys.executable, script,
        "--duration",  str(SESSION_DURATION_MIN),
        "--interval",  str(POLL_INTERVAL_SEC),
        "--output",    out,
        "--strategy",  strategy,   # ← 전략 프리셋 전달
    ]
    proc = subprocess.run(cmd, capture_output=False)

    if os.path.exists(out):
        with open(out) as f:
            return json.load(f)
    return {}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default=DEFAULT_STRATEGY,
                    choices=["safe", "balanced", "aggressive"],
                    help="전략 프리셋 (기본: balanced)")
    args = ap.parse_args()

    summary = load_summary()
    print(f"장기 누적 Papertrading 시작 | 전략: {args.strategy}")
    print(f"세션당 {SESSION_DURATION_MIN}분 | 폴링 {POLL_INTERVAL_SEC}초")
    print(f"이미 {summary['sessions']}세션 완료됨")

    session_num = summary["sessions"] + 1

    try:
        while True:
            result = run_session(session_num, strategy=args.strategy)
            if result:
                summary = update_summary(summary, result)
                print_summary(summary)
            else:
                print(f"⚠️ 세션 #{session_num} 결과 없음 — 재시도 대기 60초")
                time.sleep(60)

            session_num += 1
            print(f"\n⏸ 다음 세션까지 30초 대기...")
            time.sleep(30)

    except KeyboardInterrupt:
        print("\n🛑 장기 실행 중단")
        print_summary(summary)


if __name__ == "__main__":
    main()
