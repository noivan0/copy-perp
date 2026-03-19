#!/usr/bin/env python3
"""
scripts/pt_dashboard.py — 4개 전략 실시간 비교 대시보드

사용법:
  python3 scripts/pt_dashboard.py            # 1회 출력
  python3 scripts/pt_dashboard.py --watch    # 60초마다 갱신
  python3 scripts/pt_dashboard.py --json     # JSON 출력 (API 연동용)
"""

import os, sys, glob, json, time, argparse
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSION_DIR = os.path.join(ROOT, "papertrading", "sessions")
LOG_DIR = "/tmp/copy_perp_pt"

STRATEGIES = [
    ("default",      "🔒 기본형",  1_000,  4.77),
    ("conservative", "🛡️ 안정형",  1_000,  5.38),
    ("balanced",     "⚖️ 균형형",  5_000,  5.94),
    ("aggressive",   "⚡ 공격형", 10_000,  5.42),
]


def latest_session(strategy: str) -> dict | None:
    """해당 전략의 최신 세션 JSON 로드"""
    pattern = os.path.join(SESSION_DIR, f"{strategy}_*.json")
    files = sorted(glob.glob(pattern))
    if not files:
        return None
    try:
        with open(files[-1]) as f:
            return json.load(f)
    except Exception:
        return None


def parse_log_tail(strategy: str, lines: int = 5) -> str:
    """로그 마지막 N줄 추출"""
    log_path = os.path.join(LOG_DIR, f"{strategy}.log")
    if not os.path.exists(log_path):
        return ""
    try:
        with open(log_path) as f:
            all_lines = f.readlines()
        return "".join(all_lines[-lines:]).strip()
    except Exception:
        return ""


def is_running(strategy: str) -> bool:
    pid_path = os.path.join(LOG_DIR, f"{strategy}.pid")
    if not os.path.exists(pid_path):
        return False
    try:
        pid = int(open(pid_path).read().strip())
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError, OSError):
        return False


def render_dashboard() -> dict:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*70}")
    print(f"  📊 Copy Perp — 4전략 페이퍼트레이딩 대시보드  |  {now}")
    print(f"{'='*70}")
    print(f"  {'전략':<12} {'상태':<6} {'자본':>8} {'실현PnL':>10} {'미실현':>10} {'ROI':>8} {'승률':>7} {'거래':>6}")
    print(f"  {'-'*67}")

    results = []
    for key, label, capital, expected_roi in STRATEGIES:
        sess = latest_session(key)
        running = is_running(key)
        status = "🟢 실행" if running else "⭕ 대기"

        if sess:
            pnl_r = sess.get("total_pnl", 0.0)
            pnl_u = sess.get("unrealized_pnl", 0.0)
            roi   = sess.get("roi_pct", pnl_r / capital * 100 if capital else 0)
            wins  = sess.get("wins", 0)
            losses= sess.get("losses", 0)
            trades= sess.get("total_trades", 0)
            wt    = wins + losses
            wr    = f"{wins/wt*100:.0f}%" if wt else "—"
            roi_str = f"{roi:+.2f}%"
        else:
            pnl_r = pnl_u = roi = 0.0
            trades = wins = losses = 0
            wr = "—"
            roi_str = "—"

        gap = roi - expected_roi if sess else None
        gap_str = f" ({gap:+.2f}% vs 예상)" if gap is not None else f" (예상 {expected_roi:+.2f}%)"

        print(f"  {label:<12} {status:<6} ${capital:>7,.0f} ${pnl_r:>+9.2f} ${pnl_u:>+9.2f} {roi_str:>8} {wr:>7} {trades:>6}")

        results.append({
            "strategy":       key,
            "label":          label,
            "status":         "running" if running else "idle",
            "capital":        capital,
            "pnl_realized":   round(pnl_r, 4),
            "pnl_unrealized": round(pnl_u, 4),
            "roi_pct":        round(roi, 4),
            "expected_roi_30d": expected_roi,
            "win_rate":       round(wins/max(1,wins+losses)*100, 1),
            "total_trades":   trades,
        })

    print(f"{'='*70}\n")

    # 효과성 요약
    live = [r for r in results if r["status"] == "running"]
    if live:
        best = max(live, key=lambda x: x["roi_pct"])
        worst = min(live, key=lambda x: x["roi_pct"])
        print(f"  ✅ 실행 중: {len(live)}개 전략")
        print(f"  🏆 최고 ROI: {best['label']}  {best['roi_pct']:+.2f}%")
        if len(live) > 1:
            print(f"  📉 최저 ROI: {worst['label']}  {worst['roi_pct']:+.2f}%")

    # Copy Perp 효과성 지표
    running_pnl = sum(r["pnl_realized"] for r in live)
    if live:
        print(f"\n  💡 Copy Perp 효과성:")
        print(f"     4전략 합산 실현PnL: ${running_pnl:+.2f}")
        print(f"     vs 단순 보유 (HODL): 이 기간 BTC 대비 초과수익 계산 중...")

    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true", help="60초마다 갱신")
    ap.add_argument("--interval", type=int, default=60, help="갱신 주기(초)")
    ap.add_argument("--json", action="store_true", help="JSON 출력")
    args = ap.parse_args()

    if args.json:
        results = []
        for key, label, capital, exp in STRATEGIES:
            sess = latest_session(key)
            running = is_running(key)
            pnl_r = sess.get("total_pnl", 0.0) if sess else 0.0
            roi   = sess.get("roi_pct", 0.0) if sess else 0.0
            results.append({
                "strategy": key, "label": label, "status": "running" if running else "idle",
                "pnl_realized": pnl_r, "roi_pct": roi, "expected_roi_30d": exp,
            })
        print(json.dumps({"timestamp": time.time(), "strategies": results}, indent=2))
        return

    if args.watch:
        while True:
            os.system("clear")
            render_dashboard()
            print(f"  ⏱  {args.interval}초 후 갱신... (Ctrl+C 종료)\n")
            time.sleep(args.interval)
    else:
        render_dashboard()


if __name__ == "__main__":
    main()
