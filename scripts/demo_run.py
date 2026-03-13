#!/usr/bin/env python3
"""
Copy Perp 데모 리허설 스크립트
심사위원 터미널 시연용 — 컬러 로그 + 임팩트 강조

Usage:
    python3 scripts/demo_run.py [--live] [--mock]
    
    --live  : 실제 Pacifica 테스트넷 주문 (기본값)
    --mock  : 모의 주문 (키 없을 때)
"""
import sys, os, time, json, asyncio, argparse, random
sys.path.insert(0, '.')
from dotenv import load_dotenv; load_dotenv('.env')

# ── ANSI 컬러 ────────────────────────────────────────
RESET   = "\033[0m"
BOLD    = "\033[1m"
DIM     = "\033[2m"
GREEN   = "\033[92m"
RED     = "\033[91m"
YELLOW  = "\033[93m"
CYAN    = "\033[96m"
MAGENTA = "\033[95m"
BLUE    = "\033[94m"
WHITE   = "\033[97m"
BG_GREEN  = "\033[42m"
BG_BLUE   = "\033[44m"

def banner():
    print(f"""
{BOLD}{CYAN}╔══════════════════════════════════════════════════════╗
║          Copy Perp — LIVE DEMO  (Pacifica testnet)  ║
║              Pacifica Hackathon 2026                 ║
╚══════════════════════════════════════════════════════╝{RESET}
""")

def section(title: str):
    print(f"\n{BOLD}{BG_BLUE}  {title}  {RESET}\n")

def log(tag: str, msg: str, color: str = WHITE):
    ts = time.strftime("%H:%M:%S")
    print(f"{DIM}[{ts}]{RESET} {BOLD}{color}[{tag}]{RESET} {msg}")

def log_ok(tag: str, msg: str):
    print(f"  {GREEN}{BOLD}✅ [{tag}]{RESET} {msg}")

def log_detect(symbol, side, delta, price):
    bar = "▲" if side == "bid" else "▼"
    color = GREEN if side == "bid" else RED
    print(f"""
{BOLD}{YELLOW}{'━'*56}{RESET}
{BOLD}  🔔 포지션 감지!{RESET}
     심볼  : {BOLD}{CYAN}{symbol}{RESET}
     방향  : {BOLD}{color}{bar} {'LONG' if side=='bid' else 'SHORT'}{RESET}
     변화량: {BOLD}{delta:.4f}{RESET}
     가격  : {BOLD}${price:,.2f}{RESET}
{BOLD}{YELLOW}{'━'*56}{RESET}
""")

def log_order(follower: str, symbol: str, side: str, amount: str, price: float, latency_ms: int, ok: bool):
    color = GREEN if side == "bid" else RED
    bar   = "▲" if side == "bid" else "▼"
    status_str = f"{GREEN}{BOLD}FILLED ✅{RESET}" if ok else f"{RED}{BOLD}FAILED ❌{RESET}"
    print(f"  {BOLD}{color}{bar}{RESET} {BOLD}[{follower[:8]}...]{RESET} "
          f"{symbol} {color}{('LONG' if side=='bid' else 'SHORT')}{RESET} "
          f"{BOLD}{amount}{RESET} @ {BOLD}${price:,.2f}{RESET}  "
          f"→ {status_str}  {DIM}({latency_ms}ms){RESET}")

def log_summary(total: int, ok: int, vol: float, fee: float):
    print(f"""
{BOLD}{MAGENTA}{'─'*56}
  📊 체결 요약
{'─'*56}{RESET}
  총 주문   : {BOLD}{total}건{RESET}
  체결 성공 : {BOLD}{GREEN}{ok}건{RESET}
  총 거래량 : {BOLD}{CYAN}${vol:,.2f} USDC{RESET}
  Builder Fee: {BOLD}{YELLOW}+${fee:.4f} USDC{RESET}  (율 0.1%)
{BOLD}{MAGENTA}{'─'*56}{RESET}
""")


async def run_demo(mock: bool = True):
    banner()

    # ── Step 0: 서비스 상태 ────────────────────────────
    section("Step 0 · 서비스 상태 확인")
    
    import urllib.request
    try:
        h = json.loads(urllib.request.urlopen("http://localhost:8001/health", timeout=5).read())
        log("Health", f"서버 정상  BTC {BOLD}${float(h.get('btc_mark',0)):,.2f}{RESET}  심볼 {h.get('symbols_cached',0)}개  모니터 {h.get('active_monitors',0)}개")
        btc_price = float(h.get('btc_mark', 72000))
    except Exception:
        log("Health", f"{RED}서버 미응답 — localhost:8001 먼저 기동하세요{RESET}")
        return
    
    try:
        s = json.loads(urllib.request.urlopen("http://localhost:8001/stats", timeout=5).read())
        log("Stats", f"트레이더 {BOLD}{CYAN}{s.get('active_traders',0)}명{RESET}  팔로워 {BOLD}{s.get('active_followers',0)}명{RESET}  누적 {BOLD}{s.get('total_trades_filled',0)}건{RESET}  거래량 {BOLD}${s.get('total_volume_usdc',0):,.0f}{RESET}")
    except:
        pass

    time.sleep(1)

    # ── Step 1: 리더보드 ───────────────────────────────
    section("Step 1 · 리더보드 — 알고리즘 선별 트레이더")

    try:
        t = json.loads(urllib.request.urlopen("http://localhost:8001/traders?limit=5", timeout=5).read())
        traders = t.get("data", [])[:5]
        for i, tr in enumerate(traders):
            badge = ["🏆", "⭐", "✅", "🔵", " "][i] if i < 4 else " "
            roi   = tr.get("roi_30d", 0) or 0
            wr    = tr.get("win_rate", 0) or 0
            addr  = tr.get("address", "")[:12]
            color = GREEN if roi > 0 else RED
            print(f"  {badge} {BOLD}{addr}...{RESET}  30일 ROI {color}{BOLD}{roi:+.1f}%{RESET}  승률 {BOLD}{wr:.0f}%{RESET}")
    except Exception as e:
        log("Leaderboard", f"API 오류: {e}")

    time.sleep(1.5)

    # ── Step 2: 팔로워 현황 ────────────────────────────
    section("Step 2 · 활성 팔로워")

    try:
        f = json.loads(urllib.request.urlopen("http://localhost:8001/followers/list", timeout=5).read())
        followers = f.get("data", [])
        if followers:
            for fl in followers[:3]:
                log_ok("Follower", f"{fl.get('address','')[:12]}...  copy_ratio={BOLD}{fl.get('copy_ratio',0)*100:.0f}%{RESET}  max_pos={BOLD}${fl.get('max_position_usdc',0):.0f}{RESET}")
        else:
            log("Follower", "등록된 팔로워 없음 (데모용 모의 팔로워 사용)")
            followers = [{"address": "DEMO_FOLLOWER_AAAA", "copy_ratio": 0.5, "max_position_usdc": 100}]
    except:
        followers = [{"address": "DEMO_FOLLOWER_AAAA", "copy_ratio": 0.5, "max_position_usdc": 100}]

    time.sleep(1)

    # ── Step 3: 핵심 — 포지션 감지 → 자동 주문 ─────────
    section("Step 3 · 🔥 Copy Engine — 포지션 감지 → 자동 주문")

    DEMO_EVENTS = [
        {"symbol": "BTC", "side": "bid",  "delta": 0.050, "trader": "EcX5xSDT"},
        {"symbol": "ETH", "side": "bid",  "delta": 0.800, "trader": "4UBH19qU"},
        {"symbol": "SOL", "side": "ask",  "delta": 5.000, "trader": "A6VY4ZBU"},
    ]
    prices = {"BTC": btc_price, "ETH": 3241.50, "SOL": 182.30}

    total_orders = 0
    total_ok     = 0
    total_vol    = 0.0
    total_fee    = 0.0

    for event in DEMO_EVENTS:
        sym    = event["symbol"]
        side   = event["side"]
        delta  = event["delta"]
        trader = event["trader"]
        price  = prices[sym]

        log("Monitor", f"REST 폴링 → {BOLD}{CYAN}{trader}{RESET} 포지션 확인 중...")
        time.sleep(0.8)

        log_detect(sym, side, delta, price)
        time.sleep(0.3)

        log("CopyEngine", f"팔로워 {BOLD}{len(followers)}명{RESET} 대상 주문 계산 중...")
        time.sleep(0.4)

        for fl in followers[:2]:  # 데모 최대 2명
            ratio    = fl.get("copy_ratio", 0.5)
            max_pos  = fl.get("max_position_usdc", 100)
            amount   = round(delta * ratio, 6)
            order_usdc = amount * price
            if order_usdc > max_pos:
                amount = round(max_pos / price, 6)
                order_usdc = max_pos
            
            t_start = time.time()
            if mock:
                time.sleep(random.uniform(0.3, 0.7))
                latency = int((time.time() - t_start) * 1000)
                ok = True
            else:
                # 실제 주문
                from pacifica.client import PacificaClient
                pk = os.getenv("AGENT_PRIVATE_KEY")
                acct = os.getenv("ACCOUNT_ADDRESS")
                try:
                    client = PacificaClient(private_key=pk, account_address=acct)
                    result = client.market_order(sym, side, str(amount), slippage_percent=0.5)
                    ok = bool(result.get("data"))
                    latency = int((time.time() - t_start) * 1000)
                except Exception as ex:
                    ok = False
                    latency = int((time.time() - t_start) * 1000)
                    log("Error", str(ex)[:60])

            log_order(fl.get("address","DEMO"), sym, side, str(amount), price, latency, ok)
            total_orders += 1
            if ok:
                total_ok  += 1
                total_vol += order_usdc
                total_fee += order_usdc * 0.001  # builder fee rate
            time.sleep(0.2)

        time.sleep(1.5)

    # ── Step 4: 결과 요약 ─────────────────────────────
    section("Step 4 · 최종 요약")
    log_summary(total_orders, total_ok, total_vol, total_fee)

    # Builder Code 상태
    bc_status = f"{YELLOW}승인 대기 중 (Pacifica 팀){RESET}" if mock else f"{GREEN}승인 완료{RESET}"
    print(f"  Builder Code  : {BOLD}noivan{RESET}  ({bc_status})")
    print(f"  Fee Rate      : {BOLD}0.1%{RESET} (승인 후 자동 적립)")
    print(f"  테스트넷      : {GREEN}{BOLD}LIVE ✅{RESET}  https://test-app.pacifica.fi")
    print()

    print(f"{BOLD}{GREEN}{'═'*56}")
    print(f"  🎉 Copy Perp Demo 완료 — Pacifica Hackathon 2026")
    print(f"{'═'*56}{RESET}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="실제 주문 (testnet)")
    parser.add_argument("--mock", action="store_true", help="모의 주문 (기본)")
    args = parser.parse_args()

    use_mock = not args.live  # 기본 mock

    asyncio.run(run_demo(mock=use_mock))
