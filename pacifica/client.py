"""
Pacifica Testnet REST + WebSocket 클라이언트
SDK 기반 실제 구현
"""
import asyncio
import json
import ssl
import time
import uuid
import os
import sys

import requests
import websockets
from solders.keypair import Keypair
import base58

REST_URL = "https://test-api.pacifica.fi/api/v1"
WS_URL = "wss://test-ws.pacifica.fi/ws"
BUILDER_CODE = os.getenv("BUILDER_CODE", "")

# SSL 컨텍스트 (테스트넷 자체서명 인증서)
def _ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _sort_json_keys(value):
    if isinstance(value, dict):
        return {k: _sort_json_keys(v) for k in sorted(value.keys()) for v in [value[k]]}
    elif isinstance(value, list):
        return [_sort_json_keys(i) for i in value]
    return value


def _sign_message(header: dict, payload: dict, keypair: Keypair) -> tuple[str, str]:
    data = {**header, "data": payload}
    msg = json.dumps(_sort_json_keys(data), separators=(",", ":"))
    sig = keypair.sign_message(msg.encode("utf-8"))
    return msg, base58.b58encode(bytes(sig)).decode("ascii")


class PacificaClient:
    """
    Pacifica Testnet 클라이언트
    private_key: base58 개인키 (주문 실행 시 필요)
    """

    def __init__(self, private_key: str = None, account_address: str = None):
        if private_key:
            self.keypair = Keypair.from_base58_string(private_key)
            self.account = str(self.keypair.pubkey())
        else:
            self.keypair = None
            self.account = account_address or ""

    # ─── 조회 ────────────────────────────────────────────

    def get_info(self) -> list:
        """심볼 목록 + 스펙"""
        r = requests.get(f"{REST_URL}/info", verify=False)
        return r.json().get("data", [])

    def get_balance(self) -> float:
        """계정 잔고 (USD)"""
        r = requests.get(f"{REST_URL}/clearinghouse_state",
                         params={"account": self.account}, verify=False)
        data = r.json()
        summary = data.get("marginSummary") or data.get("crossMarginSummary", {})
        return float(summary.get("accountValue", 0))

    def get_positions(self) -> list:
        """현재 포지션 목록"""
        r = requests.get(f"{REST_URL}/clearinghouse_state",
                         params={"account": self.account}, verify=False)
        return r.json().get("assetPositions", [])

    def get_trades(self, symbol: str, limit: int = 40) -> list:
        """최근 체결 내역"""
        r = requests.get(f"{REST_URL}/trades",
                         params={"symbol": symbol}, verify=False)
        return r.json().get("data", [])[:limit]

    # ─── 주문 ────────────────────────────────────────────

    def market_order(
        self,
        symbol: str,
        side: str,           # "bid" (롱) / "ask" (숏)
        amount: str,         # USD 기준
        reduce_only: bool = False,
        slippage_percent: str = "0.5",
        builder_code: str = None,
        client_order_id: str = None,
    ) -> dict:
        """시장가 주문"""
        if not self.keypair:
            raise ValueError("주문에는 private_key가 필요합니다")

        order_id = client_order_id or str(uuid.uuid4())
        timestamp = int(time.time() * 1_000)

        header = {
            "timestamp": timestamp,
            "expiry_window": 5_000,
            "type": "create_market_order",
        }
        payload = {
            "symbol": symbol,
            "reduce_only": reduce_only,
            "amount": amount,
            "side": side,
            "slippage_percent": slippage_percent,
            "client_order_id": order_id,
        }
        if builder_code or BUILDER_CODE:
            payload["builder_code"] = builder_code or BUILDER_CODE

        _, signature = _sign_message(header, payload, self.keypair)

        request = {
            "account": self.account,
            "signature": signature,
            "timestamp": timestamp,
            "expiry_window": 5_000,
            **payload,
        }
        r = requests.post(f"{REST_URL}/orders/create_market",
                          json=request, verify=False)
        return r.json()

    def approve_builder_code(self, builder_code: str, max_fee_rate: str = "0.0003") -> dict:
        """Builder Code 승인 (팔로워 최초 1회)"""
        if not self.keypair:
            raise ValueError("승인에는 private_key가 필요합니다")

        timestamp = int(time.time() * 1_000)
        header = {
            "timestamp": timestamp,
            "expiry_window": 5_000,
            "type": "approve_builder_code",
        }
        payload = {
            "builder_code": builder_code,
            "max_fee_rate": max_fee_rate,
        }
        _, signature = _sign_message(header, payload, self.keypair)

        request = {
            "account": self.account,
            "signature": signature,
            "timestamp": timestamp,
            "expiry_window": 5_000,
            **payload,
        }
        r = requests.post(f"{REST_URL}/account/builder_codes/approve",
                          json=request, verify=False)
        return r.json()


# ─── WebSocket 가격/펀딩비 스트림 ───────────────────────

class PriceStream:
    """
    실시간 가격 + 펀딩비 + OI 스트림
    WS prices 채널에서 모든 심볼 데이터 수신
    """

    def __init__(self, on_update=None):
        self.on_update = on_update  # callback(data: list)
        self._running = False
        self.latest: dict = {}      # symbol → 최신 데이터

    async def start(self):
        self._running = True
        ssl_ctx = _ssl_ctx()
        while self._running:
            try:
                async with websockets.connect(WS_URL, ssl=ssl_ctx) as ws:
                    await ws.send(json.dumps({
                        "method": "subscribe",
                        "params": {"source": "prices"}
                    }))
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("channel") == "prices":
                            items = msg.get("data", [])
                            for item in items:
                                self.latest[item["symbol"]] = item
                            if self.on_update:
                                await self.on_update(items)
            except Exception as e:
                print(f"[PriceStream] 재연결 중: {e}")
                await asyncio.sleep(2)

    def stop(self):
        self._running = False


# ─── 포지션 폴러 (account 이벤트 WS 미지원 → REST 폴링) ───

class PositionPoller:
    """
    트레이더 포지션 변화 감지 (REST 500ms 폴링)
    """

    def __init__(self, trader_client: PacificaClient, on_change=None):
        self.client = trader_client
        self.on_change = on_change
        self._prev: list = []
        self._running = False

    async def start(self, interval: float = 0.5):
        self._running = True
        while self._running:
            try:
                curr = self.client.get_positions()
                changes = self._diff(self._prev, curr)
                for change in changes:
                    if self.on_change:
                        await self.on_change(change)
                self._prev = curr
            except Exception as e:
                print(f"[PositionPoller] 오류: {e}")
            await asyncio.sleep(interval)

    def _diff(self, prev: list, curr: list) -> list:
        prev_map = {p.get("symbol"): p for p in prev}
        curr_map = {p.get("symbol"): p for p in curr}
        changes = []

        # 새 포지션 또는 크기 변화
        for sym, pos in curr_map.items():
            prev_pos = prev_map.get(sym, {})
            prev_size = float(prev_pos.get("szi", 0) if prev_pos else 0)
            curr_size = float(pos.get("szi", 0))
            if curr_size != prev_size:
                changes.append({
                    "type": "open" if abs(curr_size) > abs(prev_size) else "reduce",
                    "symbol": sym,
                    "side": "bid" if curr_size > 0 else "ask",
                    "size_delta": abs(curr_size - prev_size),
                    "position": pos,
                })

        # 청산된 포지션
        for sym in prev_map:
            if sym not in curr_map:
                changes.append({
                    "type": "close",
                    "symbol": sym,
                    "side": "ask" if float(prev_map[sym].get("szi", 0)) > 0 else "bid",
                    "size_delta": abs(float(prev_map[sym].get("szi", 0))),
                    "position": None,
                })

        return changes

    def stop(self):
        self._running = False


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")

    # 연결 테스트
    CEO_WALLET = "3AHZqrocSguMuo9sUUP8G8YN8NwHwWV2DPUQvbDvtfaQ"
    client = PacificaClient(account_address=CEO_WALLET)

    info = client.get_info()
    print(f"✅ 테스트넷 연결 OK — {len(info)}개 심볼")

    balance = client.get_balance()
    print(f"💰 CEO 지갑 잔고: ${balance:,.2f}")

    positions = client.get_positions()
    print(f"📊 현재 포지션: {len(positions)}개")

    btc_trades = client.get_trades("BTC", limit=3)
    print(f"📈 BTC 최근 체결 3건:")
    for t in btc_trades:
        print(f"  {t['side']} ${t['price']} × {t['amount']}")

    # WS 가격 스트림 5초 테스트
    print("\n🔌 WS 가격 스트림 테스트 (5초)...")
    stream = PriceStream()
    async def run():
        task = asyncio.create_task(stream.start())
        await asyncio.sleep(5)
        stream.stop()
        task.cancel()
        btc = stream.latest.get("BTC", {})
        print(f"BTC mark: ${btc.get('mark')} | funding: {btc.get('funding')} | OI: {btc.get('open_interest')}")
    asyncio.run(run())
