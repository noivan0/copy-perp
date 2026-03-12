"""
Pacifica REST 클라이언트 — SDK 서명 방식 (urllib 기반, 방화벽 우회)
계정: env ACCOUNT_ADDRESS (기본: 3AHZqrocSguMuo9sUUP8G8YN8NwHwWV2DPUQvbDvtfaQ)
Agent: env AGENT_PRIVATE_KEY

서명 방식 (SDK utils.py 기반):
  message = sort_json_keys({header, "data": payload}) → compact JSON
  signature = keypair.sign_message(message.encode()) → base58
"""

import os
import json
import time
import uuid
import ssl
import urllib.request
import urllib.error
from typing import Optional

import base58
from solders.keypair import Keypair

REST_URL = os.getenv("PACIFICA_REST_URL", "https://test-api.pacifica.fi/api/v1")
ACCOUNT_ADDRESS = os.getenv("ACCOUNT_ADDRESS", "")
AGENT_PRIVATE_KEY = os.getenv("AGENT_PRIVATE_KEY", "")
BUILDER_CODE = os.getenv("BUILDER_CODE", "copy-perp-v1")

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def _load_keypair() -> Optional[Keypair]:
    if not AGENT_PRIVATE_KEY:
        return None
    try:
        seed = base58.b58decode(AGENT_PRIVATE_KEY)
        return Keypair.from_seed(seed[:32])
    except Exception:
        return Keypair.from_base58_string(AGENT_PRIVATE_KEY)


def _sort_json_keys(value):
    """재귀적으로 JSON 키 알파벳 정렬 (SDK utils.py 동일 로직)"""
    if isinstance(value, dict):
        return {k: _sort_json_keys(v) for k in sorted(value.keys())}
    elif isinstance(value, list):
        return [_sort_json_keys(i) for i in value]
    return value


def _sign_request(header: dict, payload: dict, keypair: Keypair) -> tuple[str, str]:
    """SDK sign_message 방식 서명 → (message, signature_base58)"""
    data = {**header, "data": payload}
    sorted_data = _sort_json_keys(data)
    message = json.dumps(sorted_data, separators=(",", ":"))
    sig_bytes = keypair.sign_message(message.encode("utf-8"))
    signature = base58.b58encode(bytes(sig_bytes)).decode("ascii")
    return message, signature


def _request(method: str, path: str, body: Optional[dict] = None) -> dict:
    url = f"{REST_URL}/{path}"
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json", "User-Agent": "CopyPerp/1.0"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()}")


class PacificaClient:
    """
    Pacifica 테스트넷 클라이언트
    - 공개 API: 서명 불필요
    - 거래 API: Agent Wallet 서명
    """

    def __init__(self, account_address: str = ACCOUNT_ADDRESS):
        self.account = account_address
        self._kp = _load_keypair()

    # ── 공개 API ──────────────────────────────────

    def get_markets(self) -> list:
        """전체 마켓 목록 (68개)"""
        return _request("GET", "info").get("data", [])

    def get_positions(self) -> list:
        """현재 포지션"""
        return _request("GET", f"positions?account={self.account}").get("data", [])

    def get_orders(self) -> list:
        """미체결 주문"""
        return _request("GET", f"orders?account={self.account}").get("data", [])

    def get_account_trades(self, limit: int = 50) -> list:
        """
        계정 체결 내역 — REST 폴링용
        스키마: {event_type, price, amount, side, cause, created_at}
        side 값: open_long / open_short / close_long / close_short
        """
        return _request("GET", f"trades?account={self.account}&limit={limit}").get("data", [])

    def get_trades(self, limit: int = 50) -> list:
        """하위 호환 — get_account_trades 위임"""
        return self.get_account_trades(limit)

    def get_market_trades(self, symbol: str, limit: int = 40) -> list:
        """마켓 전체 체결 내역 (특정 심볼)"""
        return _request("GET", f"trades?symbol={symbol}&limit={limit}").get("data", [])

    def get_orderbook(self, symbol: str) -> dict:
        return _request("GET", f"orderbook?symbol={symbol}").get("data", {})

    def get_funding_rate(self, symbol: str) -> dict:
        for m in self.get_markets():
            if m["symbol"] == symbol:
                return {
                    "symbol": symbol,
                    "funding_rate": m.get("funding_rate"),
                    "next_funding_rate": m.get("next_funding_rate"),
                }
        return {}

    # ── 서명 API ──────────────────────────────────

    def _signed_post(self, endpoint_path: str, order_type: str, payload: dict) -> dict:
        if not self._kp:
            raise RuntimeError("AGENT_PRIVATE_KEY 미설정 — 주문 실행 불가")
        timestamp = int(time.time() * 1000)
        header = {"timestamp": timestamp, "expiry_window": 5000, "type": order_type}
        _, signature = _sign_request(header, payload, self._kp)
        body = {
            "account": self.account,
            "signature": signature,
            "timestamp": timestamp,
            "expiry_window": 5000,
            **payload,
        }
        return _request("POST", endpoint_path, body)

    def market_order(
        self,
        symbol: str,
        side: str,           # "bid" (롱) / "ask" (숏)
        amount: str,
        slippage_percent: str = "0.5",
        builder_code: str = BUILDER_CODE,
        client_order_id: Optional[str] = None,
    ) -> dict:
        payload = {
            "symbol": symbol,
            "side": side,
            "amount": amount,
            "reduce_only": False,
            "slippage_percent": slippage_percent,
            "client_order_id": client_order_id or str(uuid.uuid4()),
        }
        if builder_code:
            payload["builder_code"] = builder_code
        return self._signed_post("orders/create_market", "create_market_order", payload)

    def limit_order(
        self,
        symbol: str,
        side: str,
        amount: str,
        price: str,
        post_only: bool = False,
        builder_code: str = BUILDER_CODE,
        client_order_id: Optional[str] = None,
    ) -> dict:
        payload = {
            "symbol": symbol,
            "side": side,
            "amount": amount,
            "price": price,
            "reduce_only": False,
            "post_only": post_only,
            "client_order_id": client_order_id or str(uuid.uuid4()),
        }
        if builder_code:
            payload["builder_code"] = builder_code
        return self._signed_post("orders/create", "create_limit_order", payload)

    def cancel_order(self, order_id: str) -> dict:
        return self._signed_post("orders/cancel", "cancel_order", {"order_id": order_id})

    def cancel_all_orders(self) -> dict:
        return self._signed_post("orders/cancel_all", "cancel_all_orders", {})


# ── 빠른 연결 테스트 ──────────────────────────────────
if __name__ == "__main__":
    client = PacificaClient()
    markets = client.get_markets()
    print(f"✅ 마켓: {len(markets)}개")
    btc = next((m for m in markets if m["symbol"] == "BTC"), {})
    print(f"   BTC 펀딩비: {btc.get('funding_rate')} | 레버리지: {btc.get('max_leverage')}x")
    if client.account:
        trades = client.get_account_trades(3)
        print(f"✅ 계정 체결: {len(trades)}건")
        for t in trades[:2]:
            print(f"   {t.get('side')} {t.get('amount')} @ {t.get('price')} ({t.get('event_type')})")
    print(f"✅ Agent Key: {'설정됨' if client._kp else '미설정 (읽기전용 모드)'}")
