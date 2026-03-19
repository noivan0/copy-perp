"""
CopyPerp 4전략 페이퍼트레이딩 대시보드
results/multi_pt_state.json 읽어서 현재 성과 표 출력

사용법:
    python3 scripts/pt_dashboard.py              # 1회 출력
    python3 scripts/pt_dashboard.py --watch      # 30초마다 자동 갱신
    python3 scripts/pt_dashboard.py --events 20  # 최근 이벤트 20개 출력
"""
import json
import os
import sys
import time
import argparse
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR = os.path.join(BASE_DIR, "results")
STATE_FILE = os.path.join(RESULTS_DIR, "multi_pt_state.json")
LOG_FILE = os.path.join(RESULTS_DIR, "multi_pt_log.jsonl")
PID_FILE = os.path.join(RESULTS_DIR, "multi_pt.pid")


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"❌ 상태 파일 읽기 실패: {e}")
        return {}


def load_recent_events(n: int = 10) -> list:
    if not os.path.exists(LOG_FILE):
        return []
    events = []
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except Exception:
                        pass
    except Exception:
        return []
    return events[-n:]


def get_pid_status() -> str:
    if not os.path.exists(PID_FILE):
        return "N/A"
    try:
        with open(PID_FILE, "r") as f:
            pid = int(f.read().strip())
        # 프로세스 생존 확인
        proc_exists = os.path.exists(f"/proc/{pid}")
        if proc_exists:
            return f"🟢 실행 중 (PID {pid})"
        else:
            return f"🔴 종료됨 (PID {pid})"
    except Exception as e:
        return f"❓ 불명 ({e})"


def print_dashboard(state: dict, show_events: int = 0):
    if not state or "scenarios" not in state:
        print("⏳ 페이퍼트레이딩 데이터 없음. multi_scenario_pt.py가 실행 중인지 확인하세요.")
        print(f"   상태 파일: {STATE_FILE}")
        print(f"   데몬 상태: {get_pid_status()}")
        return

    scenarios = state.get("scenarios", {})
    started_at = state.get("started_at", "")
    generated_at = state.get("generated_at", "")
    elapsed_hours = state.get("elapsed_hours", 0)
    ranking = state.get("ranking", [])

    # 시간 포맷
    try:
        start = datetime.fromisoformat(started_at)
        start_str = start.strftime("%Y-%m-%d %H:%M")
    except Exception:
        start_str = started_at[:16].replace("T", " ") if started_at else "N/A"

    try:
        gen = datetime.fromisoformat(generated_at)
        gen_str = gen.strftime("%H:%M:%S")
    except Exception:
        gen_str = generated_at[11:19] if generated_at else "N/A"

    elapsed_h = int(elapsed_hours)
    elapsed_m = int((elapsed_hours - elapsed_h) * 60)

    WIDTH = 74
    print()
    print("╔" + "═" * WIDTH + "╗")
    title = "CopyPerp 4전략 실시간 페이퍼트레이딩 대시보드"
    print(f"║{title:^{WIDTH}}║")
    time_line = f"시작: {start_str} | 경과: {elapsed_h}h {elapsed_m:02d}m | 갱신: {gen_str} UTC"
    print(f"║{time_line:^{WIDTH}}║")
    pid_line = f"데몬: {get_pid_status()}"
    print(f"║{pid_line:^{WIDTH}}║")
    print("╠" + "═" * WIDTH + "╣")
    header = f"{'전략':<12} {'자산':>10} {'PnL':>9} {'ROI':>8} {'연환산':>8} {'승률':>6} {'포지션':>6}"
    print(f"║ {header} ║")
    print("╠" + "═" * WIDTH + "╣")

    # ranking 순으로 출력 (equity 내림차순)
    display_order = ranking if ranking else list(scenarios.keys())
    for name in display_order:
        if name not in scenarios:
            continue
        data = scenarios[name]
        label = data.get("label", name)
        equity = data.get("equity", 10000)
        total_pnl = data.get("total_pnl", 0)
        roi = data.get("roi_pct", 0)
        annual = data.get("annualized_roi_pct", 0)
        win_rate = data.get("win_rate", 0)
        open_pos = data.get("open_positions", 0)
        api_ok = data.get("api_success", 0)
        api_fail = data.get("api_fail", 0)
        total_trades = data.get("total_trades", 0)

        # API 연결 모드 표시
        if api_ok > 0 and api_fail == 0:
            mode = "📡"
        elif api_ok > 0 and api_fail > 0:
            mode = "⚡"
        else:
            mode = "💾"

        equity_str = f"${equity:,.2f}"
        pnl_sign = "+" if total_pnl >= 0 else ""
        pnl_str = f"{pnl_sign}{total_pnl:.2f}"
        roi_sign = "+" if roi >= 0 else ""
        roi_str = f"{roi_sign}{roi:.3f}%"
        ann_sign = "+" if annual >= 0 else ""
        ann_str = f"{ann_sign}{annual:.1f}%"
        win_str = f"{win_rate:.0f}%"
        pos_str = f"{open_pos}개"

        row = f"{label:<12} {equity_str:>10} {pnl_str:>9} {roi_str:>8} {ann_str:>8} {win_str:>6} {pos_str:>6}"
        print(f"║ {row} {mode}║")

    print("╠" + "═" * WIDTH + "╣")
    exp_line = "예상 월 수익: 보수적 +7.8% | 기본 +13.4% | 균형 +18.3% | 적극 +33.6%"
    print(f"║{exp_line:^{WIDTH}}║")
    print("╚" + "═" * WIDTH + "╝")

    # 상세 정보 (API 상태)
    print()
    print("📊 시나리오별 상세:")
    for name in display_order:
        if name not in scenarios:
            continue
        data = scenarios[name]
        label = data.get("label", name)
        traders = data.get("traders", 0)
        total_trades = data.get("total_trades", 0)
        realized = data.get("realized_pnl", 0)
        unrealized = data.get("unrealized_pnl", 0)
        api_ok = data.get("api_success", 0)
        api_fail = data.get("api_fail", 0)
        print(
            f"  {label}: 트레이더 {traders}명 | 체결 {total_trades}회 | "
            f"실현 PnL ${realized:+.2f} | 미실현 ${unrealized:+.2f} | "
            f"API 성공/실패 {api_ok}/{api_fail}"
        )

    # 최근 이벤트
    if show_events > 0:
        events = load_recent_events(show_events)
        if events:
            print()
            print(f"📜 최근 이벤트 (최대 {show_events}건):")
            for ev in events:
                ts = ev.get("ts", "")[:19]
                scenario = ev.get("scenario", "")
                event_type = ev.get("event", "")
                symbol = ev.get("symbol", "")
                side = ev.get("side", "")
                trader = ev.get("trader", "")
                if event_type == "open":
                    usdc = ev.get("copy_usdc", 0)
                    entry = ev.get("entry", 0)
                    print(f"  [{ts}] [{scenario}] OPEN  {symbol} {side} ${usdc:.2f} @ ${entry:,.2f} [{trader}]")
                elif event_type == "close":
                    pnl = ev.get("pnl", 0)
                    pnl_sign = "+" if pnl >= 0 else ""
                    emoji = "✅" if pnl >= 0 else "❌"
                    print(f"  [{ts}] [{scenario}] {emoji} CLOSE {symbol} {side} PnL={pnl_sign}{pnl:.4f} [{trader}]")
    print()


def main():
    parser = argparse.ArgumentParser(description="CopyPerp 4전략 페이퍼트레이딩 대시보드")
    parser.add_argument("--watch", action="store_true",
                        help="30초마다 자동 갱신 (watch 모드)")
    parser.add_argument("--interval", type=int, default=30,
                        help="watch 모드 갱신 간격 초 (기본 30)")
    parser.add_argument("--events", type=int, default=10,
                        help="최근 이벤트 표시 건수 (기본 10, 0=숨김)")
    args = parser.parse_args()

    if args.watch:
        print(f"👁 Watch 모드 (갱신: {args.interval}초마다). Ctrl+C로 종료.")
        while True:
            try:
                # 화면 클리어 (선택적)
                os.system("clear" if os.name == "posix" else "cls")
                state = load_state()
                print_dashboard(state, show_events=args.events)
                time.sleep(args.interval)
            except KeyboardInterrupt:
                print("\n종료.")
                break
    else:
        state = load_state()
        print_dashboard(state, show_events=args.events)


if __name__ == "__main__":
    main()
