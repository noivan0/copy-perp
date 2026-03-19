"""
CopyPerp 추적 데몬 헬스체크
- PID 파일로 데몬 생존 확인
- 최신 스냅샷 파일 확인 (2시간 이상 신규 없으면 경고)
- 최신 성과 요약 1줄 출력

Usage: python3 scripts/heartbeat_check.py
"""

import json
import os
import glob
import time
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOTS_DIR = os.path.join(BASE_DIR, "results", "mainnet_snapshots")
PID_FILE = os.path.join(BASE_DIR, "results", "tracker_daemon.pid")
LOG_FILE = os.path.join(BASE_DIR, "results", "tracker_daemon.log")

STALE_THRESHOLD_HOURS = 2  # 2시간 이상 스냅샷 없으면 경고


def check_daemon_alive() -> tuple[bool, str]:
    """PID 파일로 데몬 생존 확인"""
    if not os.path.exists(PID_FILE):
        return False, "PID 파일 없음 (데몬 미실행)"
    try:
        with open(PID_FILE, "r") as f:
            pid = int(f.read().strip())
        # /proc/{pid} 존재 확인 (Linux)
        if os.path.exists(f"/proc/{pid}"):
            return True, f"데몬 실행 중 (PID: {pid})"
        else:
            return False, f"데몬 종료됨 (PID: {pid}, /proc/{pid} 없음)"
    except Exception as e:
        return False, f"PID 파일 읽기 오류: {e}"


def check_latest_snapshot() -> tuple[str, str]:
    """최신 스냅샷 파일 확인"""
    files = sorted(glob.glob(os.path.join(SNAPSHOTS_DIR, "*.json")))
    if not files:
        return "WARN", "스냅샷 없음 — mainnet_tracker.py 실행 필요"

    latest_file = files[-1]
    latest_mtime = os.path.getmtime(latest_file)
    age_hours = (time.time() - latest_mtime) / 3600

    try:
        with open(latest_file, "r") as f:
            snap = json.load(f)
        snap_time = snap.get("snapshot_at", "N/A")
        traders_count = snap.get("traders_total", len(snap.get("traders", [])))
        api_source = snap.get("api_source", "unknown")
    except Exception:
        snap_time = "파싱 오류"
        traders_count = 0
        api_source = "unknown"

    if age_hours > STALE_THRESHOLD_HOURS:
        status = "WARN"
        msg = f"스냅샷 오래됨 ({age_hours:.1f}시간 전) — 데몬 확인 필요"
    else:
        status = "OK"
        msg = f"최신 스냅샷: {snap_time[:16]} | 트레이더 {traders_count}명 | {api_source}"

    return status, msg


def get_performance_summary() -> str:
    """최신 스냅샷에서 성과 요약 1줄 추출"""
    files = sorted(glob.glob(os.path.join(SNAPSHOTS_DIR, "*.json")))
    if not files:
        return "데이터 없음"

    try:
        with open(files[-1], "r") as f:
            snap = json.load(f)
        traders = snap.get("traders", [])
        if not traders:
            return "트레이더 데이터 없음"

        # 상위 3명 ROI 요약
        sorted_traders = sorted(traders, key=lambda x: x.get("roi_30d", 0) or 0, reverse=True)
        top3 = sorted_traders[:3]
        summary_parts = [f"{t['address'][:8]}... ROI30d={t.get('roi_30d',0):.1f}%" for t in top3]
        return f"상위3: {' | '.join(summary_parts)}"
    except Exception as e:
        return f"요약 오류: {e}"


def check_snapshot_count() -> str:
    """스냅샷 누적 현황"""
    files = sorted(glob.glob(os.path.join(SNAPSHOTS_DIR, "*.json")))
    if not files:
        return "스냅샷: 0개"
    first = os.path.basename(files[0]).replace(".json", "")
    last = os.path.basename(files[-1]).replace(".json", "")
    return f"스냅샷: {len(files)}개 ({first} ~ {last})"


def run_heartbeat():
    """헬스체크 실행"""
    print(f"\n{'='*55}")
    print(f"=== CopyPerp 데몬 헬스체크 ===")
    print(f"시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*55}")

    # 1. 데몬 생존 확인
    daemon_ok, daemon_msg = check_daemon_alive()
    daemon_icon = "✅" if daemon_ok else "❌"
    print(f"{daemon_icon} 데몬:      {daemon_msg}")

    # 2. 스냅샷 신선도 확인
    snap_status, snap_msg = check_latest_snapshot()
    snap_icon = "✅" if snap_status == "OK" else "⚠️ "
    print(f"{snap_icon} 스냅샷:    {snap_msg}")

    # 3. 누적 현황
    count_msg = check_snapshot_count()
    print(f"📊 누적:      {count_msg}")

    # 4. 성과 요약
    perf_summary = get_performance_summary()
    print(f"📈 성과:      {perf_summary}")

    print(f"{'='*55}")

    # 종합 상태
    if daemon_ok and snap_status == "OK":
        print("→ 상태: 정상 운영 중")
    elif not daemon_ok:
        print("→ 상태: ⚠️  데몬 재시작 필요")
        print("  실행: nohup bash scripts/run_tracker_daemon.sh > results/tracker_daemon.log 2>&1 &")
    else:
        print("→ 상태: ⚠️  스냅샷 수집 지연 — 데몬 확인 필요")

    return daemon_ok, snap_status


if __name__ == "__main__":
    run_heartbeat()
