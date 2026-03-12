"""
Pacifica REST 클라이언트 — 실제 SDK 서명 방식 반영
계정: 3AHZqrocSguMuo9sUUP8G8YN8NwHwWV2DPUQvbDvtfaQ (Privy 지갑)
Agent: 5RpcRYh1Xw9pMCuAQFdTGhocmeGsEbHg36jFP6nM8DU1 (서버 서명용)

서명 방식 (SDK utils.py 기반):
- header: {timestamp, expiry_window, type}
- data: {symbol, side, amount, ...}
- message = sort_json_keys({**header, "data": payload}) → compact JSON
- signature = keypair.sign_message(message.encode()) → base58
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
    seed = base58.b58decode(AGENT_PRIVATE_KEY)
    return Keypair.from_seed(seed)


def _sort_json_keys(value):
    """재귀적으로 JSON 키 알파벳 정렬 (SDK utils.py 동일 로직)"""
    if isinstance(value, dict):
        return {k: _sort_json_keys(v) for k in sorted(value.keys())}
    elif isinstance(value, list):
        return [_sort_json_keys(i) for i in value]
    return value


def _sign_request(header: dict, payload: dict, keypair: Keypair) -> tuple[str, str]:
    """
    SDK sign_message 방식으로 서명
    Returns: (message, signature_base58)
    """
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
    - 거래 API: Agent Wallet 서명 (SDK 방식)
    """

    def __init__(self, account_address: str = ACCOUNT_ADDRESS):
        self.account = account_address
        self._kp = _load_keypair()

    # ── 공개 API ──────────────────────────────────

    def get_markets(self) -> list:
        return _request("GET", "info").get("data", [])

    def get_positions(self) -> list:
        return _request("GET", f"positions?account={self.account}").get("data", [])

    def get_orders(self) -> list:
        return _request("GET", f"orders?account={self.account}").get("data", [])

    def get_trades(self, limit: int = 50) -> list:
        return _request("GET", f"trades?account={self.account}&limit={limit}").get("data", [])

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
        """SDK 서명 방식으로 POST 요청"""
        if not self._kp:
            raise RuntimeError("AGENT_PRIVATE_KEY 미설정")

        timestamp = int(time.time() * 1000)
        header = {
            "timestamp": timestamp,
            "expiry_window": 5000,
            "type": order_type,
        }

        message, signature = _sign_request(header, payload, self._kp)

        request_body = {
            "account": self.account,
            "signature": signature,
            "timestamp": timestamp,
            "expiry_window": 5000,
            **payload,
        }

        return _request("POST", endpoint_path, request_body)

    def market_order(
        self,
        symbol: str,
        side: str,           # "bid" (롱) / "ask" (숏)
        amount: str,
        slippage_percent: str = "0.5",
        builder_code: str = BUILDER_CODE,
        client_order_id: Optional[str] = None,
    ) -> dict:
        """시장가 주문 (Builder Code 포함)"""
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
        """지정가 주문 (ALO: post_only=True)"""
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
        payload = {"order_id": order_id}
        return self._signed_post("orders/cancel", "cancel_order", payload)

    def cancel_all_orders(self) -> dict:
        payload = {}
        return self._signed_post("orders/cancel_all", "cancel_all_orders", payload)

    def get_balance(self) -> float:
        """잔고 (USDC) — 테스트넷 전용 엔드포인트 미확인, 마진 합산으로 대체"""
        positions = self.get_positions()
        return sum(float(p.get("margin", 0)) for p in positions)


# ── 테스트 ──────────────────────────────────

if __name__ == "__main__":
    client = PacificaClient()
    print(f"계정: {client.account}")
    markets = client.get_markets()
    print(f"마켓: {len(markets)}개")
    for m in markets[:3]:
        print(f"  {m['symbol']}: 펀딩비={m.get('funding_rate')}, 레버리지={m.get('max_leverage')}x")
    print(f"포지션: {len(client.get_positions())}개")
    print(f"주문: {len(client.get_orders())}개")
    trades = client.get_trades(5)
    print(f"최근 체결: {len(trades)}건")
    if trades:
        t = trades[0]
        print(f"  {t.get('side')} {t.get('amount')} @ {t.get('price')}")
    print("\n✅ 연결 정상")
