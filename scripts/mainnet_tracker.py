"""
Mainnet 트레이더 성과 누적 추적기
- 실행할 때마다 현재 스냅샷을 results/mainnet_snapshots/ 에 저장
- 여러 날 누적되면 시계열 PnL 분석 가능
- 팔로워 가상 포트폴리오 PnL 계산

Usage: python3 scripts/mainnet_tracker.py
"""

import json
import os
import sys
import ssl
import socket
import time
import logging
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mainnet_tracker")

# ── 경로 설정 ──────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEEP_ANALYSIS_PATH = os.path.join(BASE_DIR, "trader_deep_analysis.json")
SNAPSHOTS_DIR = os.path.join(BASE_DIR, "results", "mainnet_snapshots")

# ── API 설정 (기존 paper_engine.py 동일 로직) ─────────────
CF_HOST = "do5jt23sqak4.cloudfront.net"
PAC_HOST = "api.pacifica.fi"
PORT = 443

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def _cf_get(path: str, max_retries: int = 3, timeout: int = 15):
    """CloudFront SNI 우회 API 호출 (paper_engine.py 동일 로직)"""
    for attempt in range(max_retries):
        try:
            sock = socket.create_connection((CF_HOST, PORT), timeout=timeout)
            ssock = _ssl_ctx.wrap_socket(sock, server_hostname=CF_HOST)
            req = (
                f"GET /api/v1/{path} HTTP/1.1\r\n"
                f"Host: {PAC_HOST}\r\n"
                f"Accept: application/json\r\n"
                f"Connection: close\r\n\r\n"
            )
            ssock.sendall(req.encode())
            data = b""
            ssock.settimeout(timeout)
            while True:
                chunk = ssock.recv(16384)
                if not chunk:
                    break
                data += chunk
            ssock.close()
            sock.close()

            if b"\r\n\r\n" in data:
                body = data.split(b"\r\n\r\n", 1)[1]
            else:
                body = data
            # chunked encoding 처리
            if body and body[0:1].isdigit() and b"\r\n" in body[:16]:
                try:
                    size_line, rest = body.split(b"\r\n", 1)
                    chunk_size = int(size_line.strip(), 16)
                    body = rest[:chunk_size]
                except Exception:
                    pass
            return json.loads(body.decode("utf-8", "ignore"))
        except Exception as e:
            log.debug(f"API 오류 (시도 {attempt+1}/{max_retries}): {e}")
            time.sleep(1.0 * (attempt + 1))
    return None


def get_account_info(address: str) -> dict | None:
    """GET /accounts/{address} → equity, pnl 등"""
    return _cf_get(f"accounts/{address}")


def get_trade_history(address: str, limit: int = 50) -> list:
    """GET /trades/history?account={address}&limit={limit}"""
    result = _cf_get(f"trades/history?account={address}&limit={limit}")
    if result is None:
        return []
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return result.get("data", result.get("trades", result.get("history", [])))
    return []


def load_tier1_traders() -> list[dict]:
    """trader_deep_analysis.json에서 Tier1 트레이더 17명 추출"""
    with open(DEEP_ANALYSIS_PATH, "r") as f:
        data = json.load(f)
    traders = data.get("ranked_traders", [])
    tier1 = [t for t in traders if t.get("tier") == 1]
    log.info(f"Tier1 트레이더 {len(tier1)}명 로드")
    return tier1


def fetch_trader_snapshot(trader: dict, use_fallback: bool = False) -> dict:
    """트레이더 1명의 현재 메인넷 스냅샷 수집"""
    address = trader["address"]

    if use_fallback:
        # fallback: trader_deep_analysis.json 데이터 그대로 사용
        return _build_snapshot_from_local(trader, is_fallback=True)

    # 실제 API 호출 시도
    account_data = get_account_info(address)
    trade_history = get_trade_history(address, limit=50)

    if account_data is None:
        log.warning(f"  [{address[:12]}] API 실패 → 로컬 데이터 폴백")
        return _build_snapshot_from_local(trader, is_fallback=True)

    # API 응답에서 필드 추출 (응답 구조 유연하게 처리)
    def safe_float(v, default=0.0):
        try:
            return float(v) if v is not None else default
        except (ValueError, TypeError):
            return default

    equity = safe_float(account_data.get("equity", account_data.get("account_equity")))
    pnl_all_time = safe_float(account_data.get("pnl_all_time", account_data.get("total_pnl")))
    pnl_1d = safe_float(account_data.get("pnl_1d", account_data.get("daily_pnl")))
    pnl_7d = safe_float(account_data.get("pnl_7d", account_data.get("weekly_pnl")))
    pnl_30d = safe_float(account_data.get("pnl_30d", account_data.get("monthly_pnl")))

    # ROI 계산 (equity - pnl = initial_equity)
    initial_equity_30d = equity - pnl_30d if equity > 0 and pnl_30d != 0 else equity
    roi_30d = (pnl_30d / initial_equity_30d * 100) if initial_equity_30d > 0 else trader.get("roi_30d", 0)
    initial_equity_7d = equity - pnl_7d if equity > 0 and pnl_7d != 0 else equity
    roi_7d = (pnl_7d / initial_equity_7d * 100) if initial_equity_7d > 0 else trader.get("roi_7d", 0)
    roi_1d = (pnl_1d / (equity - pnl_1d) * 100) if (equity - pnl_1d) > 0 else 0

    # 거래 이력 분석
    trade_count = len(trade_history)
    last_trade_at = None
    if trade_history:
        # created_at 또는 timestamp 필드 찾기
        for t in trade_history:
            ts = t.get("created_at") or t.get("timestamp") or t.get("time")
            if ts:
                last_trade_at = ts
                break

    return {
        "address": address,
        "is_fallback": False,
        "equity": equity if equity > 0 else trader.get("equity", 0),
        "pnl_all_time": pnl_all_time if pnl_all_time != 0 else trader.get("pnl_all_time", 0),
        "pnl_1d": pnl_1d if pnl_1d != 0 else trader.get("pnl_1d", 0),
        "pnl_7d": pnl_7d if pnl_7d != 0 else trader.get("pnl_7d", 0),
        "pnl_30d": pnl_30d if pnl_30d != 0 else trader.get("pnl_30d", 0),
        "roi_30d": roi_30d if roi_30d != 0 else trader.get("roi_30d", 0),
        "roi_7d": roi_7d if roi_7d != 0 else trader.get("roi_7d", 0),
        "roi_1d": roi_1d,
        "trade_count_recent": trade_count,
        "last_trade_at": last_trade_at,
        # 추가 메타 (로컬 분석 데이터 보조)
        "sharpe_approx": trader.get("sharpe_approx", 0),
        "oi": trader.get("oi", 0),
        "final_score": trader.get("final_score", 0),
        "tier": trader.get("tier", 1),
    }


def _build_snapshot_from_local(trader: dict, is_fallback: bool = True) -> dict:
    """로컬 trader_deep_analysis.json 데이터로 스냅샷 구성"""
    return {
        "address": trader["address"],
        "is_fallback": is_fallback,
        "equity": trader.get("equity", 0),
        "pnl_all_time": trader.get("pnl_all_time", 0),
        "pnl_1d": trader.get("pnl_1d", 0),
        "pnl_7d": trader.get("pnl_7d", 0),
        "pnl_30d": trader.get("pnl_30d", 0),
        "roi_30d": trader.get("roi_30d", 0),
        "roi_7d": trader.get("roi_7d", 0),
        "roi_1d": 0.0,
        "trade_count_recent": trader.get("total_trades", 0),
        "last_trade_at": None,
        "sharpe_approx": trader.get("sharpe_approx", 0),
        "oi": trader.get("oi", 0),
        "final_score": trader.get("final_score", 0),
        "tier": trader.get("tier", 1),
    }


def run_snapshot():
    """메인 실행: 스냅샷 수집 및 저장"""
    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
    now = datetime.now()
    snapshot_time = now.strftime("%Y-%m-%dT%H:%M:%S")
    filename = now.strftime("%Y-%m-%d_%H") + ".json"
    output_path = os.path.join(SNAPSHOTS_DIR, filename)

    log.info(f"=== Mainnet 스냅샷 수집 시작: {snapshot_time} ===")

    # Tier1 트레이더 로드
    tier1_traders = load_tier1_traders()

    # API 연결 테스트
    log.info("API 연결 테스트 중...")
    test_result = _cf_get(f"accounts/{tier1_traders[0]['address']}", max_retries=1, timeout=10)
    api_available = test_result is not None
    if api_available:
        log.info("✅ API 연결 성공 — 실시간 데이터 수집")
    else:
        log.warning("⚠️  API 연결 실패 — 로컬 데이터로 폴백")

    # 각 트레이더 스냅샷 수집
    snapshots = []
    success_count = 0
    fallback_count = 0

    for i, trader in enumerate(tier1_traders):
        addr = trader["address"]
        log.info(f"  [{i+1}/{len(tier1_traders)}] {addr[:16]}... 수집 중")
        snap = fetch_trader_snapshot(trader, use_fallback=not api_available)
        snapshots.append(snap)
        if snap.get("is_fallback"):
            fallback_count += 1
        else:
            success_count += 1
        time.sleep(0.3)  # rate limit 방지

    # 스냅샷 저장
    output = {
        "snapshot_at": snapshot_time,
        "api_source": "mainnet_live" if api_available else "local_fallback",
        "traders_total": len(snapshots),
        "api_success": success_count,
        "fallback_count": fallback_count,
        "traders": snapshots,
    }

    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    log.info(f"✅ 스냅샷 저장 완료: {output_path}")
    log.info(f"   실시간 API: {success_count}명 | 폴백: {fallback_count}명")

    return output_path, output


if __name__ == "__main__":
    path, result = run_snapshot()
    print(f"\n📸 스냅샷 저장: {path}")
    print(f"   트레이더: {result['traders_total']}명")
    print(f"   데이터 소스: {result['api_source']}")
    print(f"   실시간: {result['api_success']}명 | 폴백: {result['fallback_count']}명")
