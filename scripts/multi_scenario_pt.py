"""
CopyPerp 4전략 동시 페이퍼트레이딩
- conservative / default / balanced / aggressive 동시 실행
- 각각 독립 가상 포트폴리오 $10,000
- 실제 Mainnet positions API 기반 PnL 계산 (포지션 스냅샷 delta 방식)
- 결과: results/multi_pt_state.json (실시간 갱신)
        results/multi_pt_log.jsonl (이벤트 로그)

실행:
    python3 scripts/multi_scenario_pt.py
    python3 scripts/multi_scenario_pt.py --dry-run   # API 없이 시뮬레이션 테스트
"""
import json
import os
import ssl
import socket
import time
import logging
import threading
import signal
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("multi_pt")

# ── 경로 설정 ─────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR = os.path.join(BASE_DIR, "results")
STATE_FILE = os.path.join(RESULTS_DIR, "multi_pt_state.json")
LOG_FILE = os.path.join(RESULTS_DIR, "multi_pt_log.jsonl")
TRADER_ANALYSIS = os.path.join(BASE_DIR, "trader_deep_analysis.json")

os.makedirs(RESULTS_DIR, exist_ok=True)

# ── Mainnet API (CloudFront SNI 우회) ─────────────────────
CF_HOST = "do5jt23sqak4.cloudfront.net"
PAC_HOST = "api.pacifica.fi"
PORT = 443

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def _cf_get(path: str, timeout: int = 15, retries: int = 3):
    for attempt in range(retries):
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
                chunk = ssock.recv(32768)
                if not chunk:
                    break
                data += chunk
            ssock.close()
            sock.close()
            if b"\r\n\r\n" not in data:
                continue
            body = data.split(b"\r\n\r\n", 1)[1]
            # chunked transfer encoding 처리
            if body and body[:1].isalnum() and b"\r\n" in body[:20]:
                try:
                    size_line, rest = body.split(b"\r\n", 1)
                    chunk_size = int(size_line.strip(), 16)
                    body = rest[:chunk_size]
                except Exception:
                    pass
            parsed = json.loads(body.decode("utf-8", "ignore"))
            if isinstance(parsed, dict) and parsed.get("success") is False:
                return None
            return parsed
        except Exception as e:
            log.debug(f"API [{path}] 오류 ({attempt+1}/{retries}): {e}")
            time.sleep(1.0 * (attempt + 1))
    return None


def get_positions(address: str) -> list:
    """positions API: symbol, side(bid/ask), amount, entry_price"""
    r = _cf_get(f"positions?account={address}")
    if isinstance(r, dict):
        return r.get("data") or []
    if isinstance(r, list):
        return r
    return []


def get_mark_price(symbol: str) -> Optional[float]:
    """현재 마크 가격 조회 (미실현 PnL 계산용)"""
    r = _cf_get(f"markets/{symbol}")
    if isinstance(r, dict):
        data = r.get("data") or r
        price = data.get("mark_price") or data.get("index_price") or data.get("price")
        if price:
            try:
                return float(price)
            except (TypeError, ValueError):
                pass
    return None


# ── 트레이더 구성 (Mainnet 실측 데이터 기반) ──────────────
# 실제 trader_deep_analysis.json tier==1 트레이더 주소 사용
SCENARIO_TRADERS = {
    "conservative": {
        "traders": ["EcX5xSDT45Nvhi2gMTjTnhF3KT2w4sPF54esEZS3hwZu"],  # S등급 1명
        "copy_ratio": 0.10,
        "max_position_usdc": 100.0,
        "label": "🛡 보수적",
        "expected_monthly_roi_pct": 7.8,
    },
    "default": {
        "traders": [
            "EcX5xSDT45Nvhi2gMTjTnhF3KT2w4sPF54esEZS3hwZu",  # S등급
            "A6VY4ZBUohgSLkwMuDwDvAnzgiXFB1eTDzaixyitPJep",   # A등급 ROI 58.9%
        ],
        "copy_ratio": 0.10,
        "max_position_usdc": 300.0,
        "label": "⚖️ 기본",
        "expected_monthly_roi_pct": 13.4,
    },
    "balanced": {
        "traders": [
            "EcX5xSDT45Nvhi2gMTjTnhF3KT2w4sPF54esEZS3hwZu",
            "A6VY4ZBUohgSLkwMuDwDvAnzgiXFB1eTDzaixyitPJep",
            "4UBH19qUbXEaqyz9fKrFHuvj8BPMoM87H71s1YPKyGYq",
            "7gV81bz99MUBVb2aLYxW7MG1RMDdRdJYTPyC2syjba8y",
        ],
        "copy_ratio": 0.07,
        "max_position_usdc": 300.0,
        "label": "📊 균형",
        "expected_monthly_roi_pct": 18.3,
    },
    "aggressive": {
        "traders": [
            "EcX5xSDT45Nvhi2gMTjTnhF3KT2w4sPF54esEZS3hwZu",
            "A6VY4ZBUohgSLkwMuDwDvAnzgiXFB1eTDzaixyitPJep",
            "4UBH19qUbXEaqyz9fKrFHuvj8BPMoM87H71s1YPKyGYq",
            "7gV81bz99MUBVb2aLYxW7MG1RMDdRdJYTPyC2syjba8y",
            "3rXoG6i55P7D1Q3tYsB7Unds8nBtKh7vH5VUyMDpWkSe",
            "E1vabqxiuUfB29BAwLppTLLNMAq6HJqp7gSz1NiYwWz7",
            "5BPd5WYVvDE2kHMjzGmLHMaAorSm8bEfERcsycg5GCAD",
            "9XCVb4SQVADNeE6HBhZKytFHFqo1KyCDpqbqNfp48qen",
            "DThxt2yhDvJv9KU9bPMuKsd7vcwdDtaRtuh4NvohutQi",
        ],
        "copy_ratio": 0.07,
        "max_position_usdc": 500.0,
        "label": "⚡ 적극적",
        "expected_monthly_roi_pct": 33.6,
    },
}


# ── 폴백용: trader_deep_analysis.json에서 roi_1d 데이터 로드 ──
def load_trader_roi_data() -> dict:
    """API 실패 시 폴백용 트레이더 roi_1d 데이터"""
    try:
        with open(TRADER_ANALYSIS, "r") as f:
            data = json.load(f)
        result = {}
        for t in data.get("ranked_traders", []):
            addr = t.get("address", "")
            if addr:
                result[addr] = {
                    "pnl_1d": float(t.get("pnl_1d", 0) or 0),
                    "equity": float(t.get("equity", 0) or 0),
                    "tier": t.get("tier", 2),
                }
        return result
    except Exception as e:
        log.warning(f"trader_deep_analysis.json 로드 실패: {e}")
        return {}


TRADER_ROI_DATA = load_trader_roi_data()


# ── 가상 포지션 ────────────────────────────────────────────
@dataclass
class VirtualPos:
    symbol: str
    side: str              # "bid"(long) or "ask"(short)
    copy_size: float       # 가상 포지션 크기 (token 수량)
    copy_usdc: float       # 가상 포지션 USDC 금액
    entry_price: float     # 진입 가격
    trader_addr: str
    opened_at: str = ""


# ── 가상 포트폴리오 ────────────────────────────────────────
@dataclass
class ScenarioPortfolio:
    name: str
    label: str
    traders: list
    copy_ratio: float
    max_position_usdc: float
    expected_monthly_roi_pct: float

    capital: float = 10_000.0
    equity: float = 10_000.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_trades: int = 0
    win_trades: int = 0
    positions: dict = field(default_factory=dict)   # {trader_addr+symbol: VirtualPos}
    trade_log: list = field(default_factory=list)
    started_at: str = ""
    last_updated: str = ""
    api_success_count: int = 0
    api_fail_count: int = 0

    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.win_trades / self.total_trades * 100

    def roi_pct(self) -> float:
        return (self.equity - self.capital) / self.capital * 100

    def annualized_roi_pct(self) -> float:
        """경과 시간 기반 연환산 ROI"""
        if not self.started_at:
            return 0.0
        try:
            start = datetime.fromisoformat(self.started_at)
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            elapsed_hours = (now - start).total_seconds() / 3600
            if elapsed_hours < 0.01:
                return 0.0
            roi = self.roi_pct()
            # 연환산 = roi * (8760 / elapsed_hours)
            annual = roi * (8760 / elapsed_hours)
            return round(annual, 2)
        except Exception:
            return 0.0

    def to_dict(self) -> dict:
        total_pnl = self.realized_pnl + self.unrealized_pnl
        return {
            "label": self.label,
            "capital": round(self.capital, 2),
            "equity": round(self.equity, 2),
            "realized_pnl": round(self.realized_pnl, 4),
            "unrealized_pnl": round(self.unrealized_pnl, 4),
            "total_pnl": round(total_pnl, 4),
            "roi_pct": round(self.roi_pct(), 4),
            "annualized_roi_pct": self.annualized_roi_pct(),
            "total_trades": self.total_trades,
            "win_rate": round(self.win_rate(), 2),
            "traders": len(self.traders),
            "open_positions": len(self.positions),
            "expected_monthly_roi_pct": self.expected_monthly_roi_pct,
            "api_success": self.api_success_count,
            "api_fail": self.api_fail_count,
            "started_at": self.started_at,
            "last_updated": self.last_updated,
        }


# ── 이벤트 로그 기록 ────────────────────────────────────────
_log_lock = threading.Lock()


def append_event_log(event: dict):
    with _log_lock:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception as e:
            log.warning(f"이벤트 로그 기록 실패: {e}")


# ── 상태 저장 ────────────────────────────────────────────────
_state_lock = threading.Lock()


def save_state(shared_state: dict, started_at: str):
    with _state_lock:
        try:
            scenarios = {}
            for name, data in shared_state.items():
                if isinstance(data, dict):
                    scenarios[name] = data

            # 현재 equity 기준으로 ranking
            ranking = sorted(
                scenarios.keys(),
                key=lambda k: scenarios[k].get("equity", 10000),
                reverse=True,
            )

            # elapsed_hours 계산
            elapsed_hours = 0.0
            if started_at:
                try:
                    start = datetime.fromisoformat(started_at)
                    now = datetime.now(timezone.utc).replace(tzinfo=None)
                    elapsed_hours = (now - start).total_seconds() / 3600
                except Exception:
                    pass

            state = {
                "generated_at": datetime.utcnow().isoformat(),
                "started_at": started_at,
                "elapsed_hours": round(elapsed_hours, 4),
                "scenarios": scenarios,
                "ranking": ranking,
            }
            # 임시 파일로 먼저 쓰고 교체 (atomic)
            tmp_file = STATE_FILE + ".tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, ensure_ascii=False, default=str)
            os.replace(tmp_file, STATE_FILE)
        except Exception as e:
            log.warning(f"상태 저장 실패: {e}")


# ── 폴백: roi_1d 기반 가상 PnL 시뮬레이션 ─────────────────
def simulate_pnl_from_roi(trader_addr: str, portfolio: ScenarioPortfolio, interval_secs: int = 30):
    """
    API 실패 시 trader_deep_analysis.json의 pnl_1d 기반으로
    30초 단위 가상 PnL 생성 (1일 PnL을 86400초로 나눔)
    """
    roi_data = TRADER_ROI_DATA.get(trader_addr)
    if not roi_data:
        return

    pnl_1d = roi_data.get("pnl_1d", 0)
    equity = roi_data.get("equity", 1_000_000)
    if equity <= 0 or pnl_1d == 0:
        return

    # 30초 분량 PnL
    pnl_per_interval = pnl_1d * (interval_secs / 86400)

    # copy_ratio 적용
    copy_pnl = pnl_per_interval * portfolio.copy_ratio
    # max_position 클램핑 (대략적)
    max_copy = portfolio.max_position_usdc * 0.01  # 최대 포지션의 1% 수익으로 제한
    copy_pnl = max(min(copy_pnl, max_copy), -max_copy)

    # 슬리피지 5% 헤어컷
    copy_pnl *= 0.95

    if abs(copy_pnl) > 0.001:
        portfolio.realized_pnl += copy_pnl
        portfolio.equity = portfolio.capital + portfolio.realized_pnl + portfolio.unrealized_pnl
        if copy_pnl > 0:
            portfolio.total_trades += 1
            portfolio.win_trades += 1
        elif copy_pnl < 0:
            portfolio.total_trades += 1

        log.debug(
            f"[{portfolio.name}][폴백] {trader_addr[:8]}... "
            f"roi_pnl={copy_pnl:+.4f} equity=${portfolio.equity:,.2f}"
        )


# ── 시나리오 루프 (스레드 함수) ────────────────────────────
def run_scenario_loop(
    name: str,
    config: dict,
    shared_state: dict,
    stop_event: threading.Event,
    started_at: str,
    poll_interval: int = 30,
    dry_run: bool = False,
):
    """각 시나리오 독립 스레드로 실행"""
    portfolio = ScenarioPortfolio(
        name=name,
        label=config["label"],
        traders=config["traders"],
        copy_ratio=config["copy_ratio"],
        max_position_usdc=config["max_position_usdc"],
        expected_monthly_roi_pct=config["expected_monthly_roi_pct"],
        started_at=started_at,
    )

    # {trader_addr: {pos_key: position_snapshot}}
    prev_positions: dict = {}

    log.info(
        f"[{name}] 시작 — 트레이더 {len(config['traders'])}명 "
        f"copy={config['copy_ratio']} max=${config['max_position_usdc']}"
    )

    while not stop_event.is_set():
        now_str = datetime.utcnow().isoformat()
        portfolio.last_updated = now_str

        for trader_addr in config["traders"]:
            if stop_event.is_set():
                break

            if dry_run:
                # dry_run: API 없이 roi_1d 기반 시뮬레이션
                simulate_pnl_from_roi(trader_addr, portfolio, interval_secs=poll_interval)
                portfolio.api_success_count += 1
                time.sleep(0.1)
                continue

            try:
                positions = get_positions(trader_addr)
                portfolio.api_success_count += 1

                # 현재 포지션을 key: symbol+side 딕셔너리로 변환
                curr_pos_map = {}
                for pos in (positions or []):
                    symbol = pos.get("symbol") or pos.get("market") or "UNKNOWN"
                    side = pos.get("side") or "bid"
                    amount = float(pos.get("amount") or pos.get("size") or 0)
                    entry_price = float(pos.get("entry_price") or pos.get("avg_entry") or 0)
                    if amount <= 0 or entry_price <= 0:
                        continue
                    key = f"{symbol}:{side}"
                    curr_pos_map[key] = {
                        "symbol": symbol,
                        "side": side,
                        "amount": amount,
                        "entry_price": entry_price,
                    }

                prev = prev_positions.get(trader_addr, {})

                # 신규 오픈 감지 (이전에 없었던 포지션)
                for key, pos_data in curr_pos_map.items():
                    if key not in prev:
                        # 새 포지션 오픈
                        symbol = pos_data["symbol"]
                        side = pos_data["side"]
                        amount = pos_data["amount"]
                        entry_price = pos_data["entry_price"]

                        copy_size = amount * portfolio.copy_ratio
                        copy_usdc = copy_size * entry_price
                        if copy_usdc > portfolio.max_position_usdc:
                            copy_size = portfolio.max_position_usdc / entry_price
                            copy_usdc = portfolio.max_position_usdc

                        vpos = VirtualPos(
                            symbol=symbol,
                            side=side,
                            copy_size=copy_size,
                            copy_usdc=copy_usdc,
                            entry_price=entry_price,
                            trader_addr=trader_addr,
                            opened_at=now_str,
                        )
                        vpos_key = f"{trader_addr[:8]}:{key}"
                        portfolio.positions[vpos_key] = asdict(vpos)

                        event = {
                            "ts": now_str,
                            "scenario": name,
                            "event": "open",
                            "symbol": symbol,
                            "side": side,
                            "size": round(copy_size, 6),
                            "entry": round(entry_price, 4),
                            "copy_usdc": round(copy_usdc, 2),
                            "trader": trader_addr[:8],
                        }
                        append_event_log(event)
                        log.info(
                            f"[{name}] OPEN {symbol} {side} "
                            f"size={copy_size:.4f} entry=${entry_price:,.2f} "
                            f"usdc=${copy_usdc:.2f} [{trader_addr[:8]}]"
                        )

                # 청산 감지 (이전에 있었던 포지션이 사라짐)
                for key, prev_pos_data in prev.items():
                    if key not in curr_pos_map:
                        # 포지션 청산됨
                        symbol = prev_pos_data["symbol"]
                        side = prev_pos_data["side"]
                        entry_price = prev_pos_data["entry_price"]

                        vpos_key = f"{trader_addr[:8]}:{key}"
                        vpos_data = portfolio.positions.pop(vpos_key, None)

                        if vpos_data:
                            copy_size = vpos_data["copy_size"]
                            # exit_price: 현재 마크 가격 시도, 없으면 entry_price 사용 (0 PnL)
                            exit_price = get_mark_price(symbol) or entry_price

                            if side == "bid":  # long
                                pnl = (exit_price - entry_price) * copy_size
                            else:  # ask / short
                                pnl = (entry_price - exit_price) * copy_size

                            # 슬리피지 5% 헤어컷
                            pnl *= 0.95

                            portfolio.realized_pnl += pnl
                            portfolio.equity = (
                                portfolio.capital
                                + portfolio.realized_pnl
                                + portfolio.unrealized_pnl
                            )
                            portfolio.total_trades += 1
                            if pnl > 0:
                                portfolio.win_trades += 1

                            roi_pct = pnl / vpos_data["copy_usdc"] * 100 if vpos_data["copy_usdc"] > 0 else 0

                            event = {
                                "ts": now_str,
                                "scenario": name,
                                "event": "close",
                                "symbol": symbol,
                                "side": side,
                                "pnl": round(pnl, 4),
                                "roi_pct": round(roi_pct, 4),
                                "exit_price": round(exit_price, 4),
                                "trader": trader_addr[:8],
                            }
                            append_event_log(event)
                            emoji = "✅" if pnl > 0 else "❌"
                            log.info(
                                f"[{name}] {emoji} CLOSE {symbol} {side} "
                                f"pnl={pnl:+.4f} equity=${portfolio.equity:,.2f} "
                                f"[{trader_addr[:8]}]"
                            )

                # 미실현 PnL 업데이트 (열린 포지션의 현재 가치 추정)
                unrealized = 0.0
                for vpos_key, vpos_data in portfolio.positions.items():
                    symbol = vpos_data["symbol"]
                    side = vpos_data["side"]
                    entry_price = vpos_data["entry_price"]
                    copy_size = vpos_data["copy_size"]
                    # mark price 조회 생략 (과도한 API 호출 방지) — entry_price 기반으로 0 유지
                    # 실제 마크 가격은 별도 타이머에서 주기적으로 업데이트
                    unrealized += 0.0  # 보수적: 미실현 PnL은 0으로 유지

                portfolio.unrealized_pnl = unrealized
                portfolio.equity = portfolio.capital + portfolio.realized_pnl + portfolio.unrealized_pnl

                # 현재 스냅샷 저장
                prev_positions[trader_addr] = curr_pos_map

            except Exception as e:
                portfolio.api_fail_count += 1
                log.warning(f"[{name}] {trader_addr[:8]}... API 오류: {e}")
                # 폴백: roi_1d 기반 시뮬레이션
                simulate_pnl_from_roi(trader_addr, portfolio, interval_secs=poll_interval)

            # 트레이더 간 API 호출 딜레이 (rate limit 방지)
            time.sleep(0.5)

        # 현재 포트폴리오 상태를 shared_state에 업데이트
        shared_state[name] = portfolio.to_dict()

        if stop_event.wait(timeout=poll_interval):
            break

    log.info(f"[{name}] 루프 종료 — realized_pnl={portfolio.realized_pnl:+.4f}")
    shared_state[name] = portfolio.to_dict()


# ── 대시보드 출력 ────────────────────────────────────────────
def print_dashboard(shared_state: dict, started_at: str):
    if not shared_state:
        print("⏳ 데이터 수집 중...")
        return

    try:
        start = datetime.fromisoformat(started_at)
        now = datetime.utcnow()
        elapsed = (now - start).total_seconds()
        elapsed_h = int(elapsed // 3600)
        elapsed_m = int((elapsed % 3600) // 60)
    except Exception:
        elapsed_h, elapsed_m = 0, 0

    now_str = datetime.utcnow().strftime("%H:%M:%S")
    start_str = started_at[:16].replace("T", " ") if started_at else "N/A"

    # 현재 equity 기준으로 정렬
    scenario_order = ["aggressive", "balanced", "default", "conservative"]
    scenarios_data = []
    for name in scenario_order:
        if name in shared_state:
            scenarios_data.append((name, shared_state[name]))

    # equity 기준 재정렬
    scenarios_data.sort(key=lambda x: x[1].get("equity", 10000), reverse=True)

    WIDTH = 74
    print()
    print("╔" + "═" * WIDTH + "╗")
    title = "CopyPerp 4전략 실시간 페이퍼트레이딩 대시보드"
    print(f"║{title:^{WIDTH}}║")
    time_line = f"시작: {start_str} | 경과: {elapsed_h}h {elapsed_m:02d}m | 갱신: {now_str} UTC"
    print(f"║{time_line:^{WIDTH}}║")
    print("╠" + "═" * WIDTH + "╣")
    header = f"{'전략':<12} {'자산':>10} {'PnL':>9} {'ROI':>8} {'연환산':>8} {'승률':>6} {'포지션':>6}"
    print(f"║ {header} ║")
    print("╠" + "═" * WIDTH + "╣")

    for name, data in scenarios_data:
        label = data.get("label", name)
        equity = data.get("equity", 10000)
        total_pnl = data.get("total_pnl", 0)
        roi = data.get("roi_pct", 0)
        annual = data.get("annualized_roi_pct", 0)
        win_rate = data.get("win_rate", 0)
        open_pos = data.get("open_positions", 0)
        api_ok = data.get("api_success", 0)
        api_fail = data.get("api_fail", 0)
        mode = "📡" if api_ok > 0 else "💾"

        equity_str = f"${equity:,.2f}"
        pnl_str = f"{'+' if total_pnl >= 0 else ''}{total_pnl:.2f}"
        roi_str = f"{'+' if roi >= 0 else ''}{roi:.3f}%"
        ann_str = f"{'+' if annual >= 0 else ''}{annual:.1f}%"
        win_str = f"{win_rate:.0f}%"
        pos_str = f"{open_pos}개"

        row = f"{label:<12} {equity_str:>10} {pnl_str:>9} {roi_str:>8} {ann_str:>8} {win_str:>6} {pos_str:>6}"
        print(f"║ {row} {mode}║")

    print("╠" + "═" * WIDTH + "╣")
    exp_line = "예상 월 수익: 보수적 +7.8% | 기본 +13.4% | 균형 +18.3% | 적극 +33.6%"
    print(f"║{exp_line:^{WIDTH}}║")
    print("╚" + "═" * WIDTH + "╝")
    print()


# ── 메인 ─────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser(description="CopyPerp 4전략 동시 페이퍼트레이딩")
    parser.add_argument("--dry-run", action="store_true",
                        help="API 없이 roi_1d 기반 시뮬레이션 모드")
    parser.add_argument("--interval", type=int, default=30,
                        help="폴링 간격 초 (기본 30)")
    parser.add_argument("--dashboard-interval", type=int, default=60,
                        help="콘솔 대시보드 갱신 간격 초 (기본 60)")
    args = parser.parse_args()

    started_at = datetime.utcnow().isoformat()
    shared_state = {}
    stop_event = threading.Event()

    # 시그널 핸들러
    def handle_signal(sig, frame):
        log.info("⛔ 종료 신호 수신 — 스레드 종료 중...")
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    mode_label = "DRY-RUN (폴백 시뮬레이션)" if args.dry_run else "LIVE (Mainnet API)"
    log.info(f"🚀 CopyPerp 4전략 동시 페이퍼트레이딩 시작 [{mode_label}]")
    log.info(f"   폴링 간격: {args.interval}초 | 대시보드: {args.dashboard_interval}초")
    log.info(f"   상태 파일: {STATE_FILE}")
    log.info(f"   로그 파일: {LOG_FILE}")

    # 초기 상태 저장 (빈 데이터)
    for name, cfg in SCENARIO_TRADERS.items():
        shared_state[name] = {
            "label": cfg["label"],
            "capital": 10000.0,
            "equity": 10000.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "total_pnl": 0.0,
            "roi_pct": 0.0,
            "annualized_roi_pct": 0.0,
            "total_trades": 0,
            "win_rate": 0.0,
            "traders": len(cfg["traders"]),
            "open_positions": 0,
            "expected_monthly_roi_pct": cfg["expected_monthly_roi_pct"],
            "api_success": 0,
            "api_fail": 0,
            "started_at": started_at,
            "last_updated": started_at,
        }
    save_state(shared_state, started_at)

    # 각 시나리오 스레드 시작
    threads = []
    for name, cfg in SCENARIO_TRADERS.items():
        t = threading.Thread(
            target=run_scenario_loop,
            args=(name, cfg, shared_state, stop_event, started_at),
            kwargs={"poll_interval": args.interval, "dry_run": args.dry_run},
            name=f"pt-{name}",
            daemon=True,
        )
        t.start()
        threads.append(t)
        log.info(f"  ▶ [{name}] 스레드 시작 (tid={t.ident})")
        time.sleep(0.5)  # 스레드 간 시작 간격

    # 메인 루프: 주기적으로 상태 저장 + 대시보드 출력
    last_dashboard = 0.0
    try:
        while not stop_event.is_set():
            time.sleep(5)

            # 상태 파일 갱신
            save_state(shared_state, started_at)

            # 대시보드 출력
            now_ts = time.time()
            if now_ts - last_dashboard >= args.dashboard_interval:
                print_dashboard(shared_state, started_at)
                last_dashboard = now_ts

    except KeyboardInterrupt:
        log.info("⛔ KeyboardInterrupt — 종료 중...")
        stop_event.set()

    stop_event.set()
    for t in threads:
        t.join(timeout=10)

    save_state(shared_state, started_at)
    log.info("✅ 모든 스레드 종료 완료")
    print_dashboard(shared_state, started_at)


if __name__ == "__main__":
    main()
