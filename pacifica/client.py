"""
Pacifica REST 클라이언트 — 테스트넷 연동
계정: 3AHZqrocSguMuo9sUUP8G8YN8NwHwWV2DPUQvbDvtfaQ (Privy 지갑)
Agent: 5RpcRYh1Xw9pMCuAQFdTGhocmeGsEbHg36jFP6nM8DU1 (서버 서명용)
"""
import os
import json
import time
import uuid
import hashlib
import ssl
import urllib.request
import urllib.error
from typing import Optional

from solders.keypair import Keypair
import base58


REST_URL = os.getenv("PACIFICA_REST_URL", "https://test-api.pacifica.fi/api/v1")
ACCOUNT_ADDRESS = os.getenv("ACCOUNT_ADDRESS", "")
AGENT_PRIVATE_KEY = os.getenv("AGENT_PRIVATE_KEY", "")
BUILDER_CODE = os.getenv("BUILDER_CODE", "")

# SSL 검증 설정 (테스트넷 self-signed cert 대응)
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def _load_keypair() -> Optional[Keypair]:
    """Agent 개인키 로드"""
    if not AGENT_PRIVATE_KEY:
        return None
    seed = base58.b58decode(AGENT_PRIVATE_KEY)
    return Keypair.from_seed(seed)


def _sign_request(payload: dict, keypair: Keypair) -> str:
    """요청 서명 (sort_keys=True, 5초 만료)"""
    msg = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    msg_bytes = msg.encode()
    sig = keypair.sign_message(msg_bytes)
    return str(sig)


def _request(method: str, path: str, body: Optional[dict] = None) -> dict:
    """HTTP 요청"""
    url = f"{REST_URL}/{path}"
    data = json.dumps(body).encode() if body else None
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "CopyPerp/1.0",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body_str = e.read().decode()
        raise RuntimeError(f"HTTP {e.code}: {body_str}")


class PacificaClient:
    """
    Pacifica 테스트넷 클라이언트
    공개 엔드포인트: 서명 불필요
    거래 엔드포인트: Agent Wallet 서명 필요
    """

    def __init__(self, account_address: str = ACCOUNT_ADDRESS):
        self.account = account_address
        self._kp = _load_keypair()

    # ──────────────────────────────────────────
    # 공개 API (서명 불필요)
    # ──────────────────────────────────────────

    def get_markets(self) -> list:
        """전체 마켓 정보"""
        r = _request("GET", "info")
        return r.get("data", [])

    def get_positions(self) -> list:
        """열린 포지션 목록"""
        r = _request("GET", f"positions?account={self.account}")
        return r.get("data", [])

    def get_orders(self) -> list:
        """미체결 주문 목록"""
        r = _request("GET", f"orders?account={self.account}")
        return r.get("data", [])

    def get_trades(self, limit: int = 50) -> list:
        """체결 내역"""
        r = _request("GET", f"trades?account={self.account}&limit={limit}")
        return r.get("data", [])

    def get_orderbook(self, symbol: str) -> dict:
        """오더북"""
        r = _request("GET", f"orderbook?symbol={symbol}")
        return r.get("data", {})

    def get_funding_rate(self, symbol: str) -> dict:
        """펀딩비"""
        markets = self.get_markets()
        for m in markets:
            if m["symbol"] == symbol:
                return {
                    "symbol": symbol,
                    "funding_rate": m.get("funding_rate"),
                    "next_funding_rate": m.get("next_funding_rate"),
                }
        return {}

    # ──────────────────────────────────────────
    # 서명 필요 API (Agent Wallet)
    # ──────────────────────────────────────────

    def _signed_request(self, path: str, payload: dict) -> dict:
        """Agent 서명이 포함된 POST 요청"""
        if not self._kp:
            raise RuntimeError("AGENT_PRIVATE_KEY가 설정되지 않았습니다")

        payload["timestamp"] = int(time.time() * 1000)
        payload["expiry"] = payload["timestamp"] + 5000  # 5초 만료
        payload["nonce"] = str(uuid.uuid4())

        sig = _sign_request(payload, self._kp)
        payload["signature"] = sig
        payload["signer"] = str(self._kp.pubkey())

        return _request("POST", path, payload)

    def market_order(
        self,
        symbol: str,
        side: str,  # "bid" (롱) / "ask" (숏)
        amount: str,
        leverage: int = 2,
        builder_code: str = BUILDER_CODE,
        client_order_id: Optional[str] = None,
    ) -> dict:
        """시장가 주문"""
        payload = {
            "account": self.account,
            "symbol": symbol,
            "side": side,
            "amount": amount,
            "order_type": "market",
            "leverage": leverage,
            "margin_type": "isolated",
            "client_order_id": client_order_id or str(uuid.uuid4()),
        }
        if builder_code:
            payload["builder_code"] = builder_code
        return self._signed_request("order", payload)

    def limit_order(
        self,
        symbol: str,
        side: str,
        amount: str,
        price: str,
        leverage: int = 2,
        post_only: bool = False,
        builder_code: str = BUILDER_CODE,
        client_order_id: Optional[str] = None,
    ) -> dict:
        """지정가 주문 (ALO 지원)"""
        payload = {
            "account": self.account,
            "symbol": symbol,
            "side": side,
            "amount": amount,
            "price": price,
            "order_type": "limit",
            "leverage": leverage,
            "margin_type": "isolated",
            "post_only": post_only,
            "client_order_id": client_order_id or str(uuid.uuid4()),
        }
        if builder_code:
            payload["builder_code"] = builder_code
        return self._signed_request("order", payload)

    def cancel_order(self, order_id: str) -> dict:
        """주문 취소"""
        payload = {
            "account": self.account,
            "order_id": order_id,
        }
        return self._signed_request("cancel", payload)

    def get_balance(self) -> float:
        """계정 잔고 (USDC)"""
        # 테스트넷에서 확인된 엔드포인트 없음 → 추후 확인 필요
        # 임시: 포지션에서 마진 합산
        positions = self.get_positions()
        total_margin = sum(float(p.get("margin", 0)) for p in positions)
        return total_margin


# ──────────────────────────────────────────
# 테스트
# ──────────────────────────────────────────

if __name__ == "__main__":
    client = PacificaClient()

    print("=== Pacifica 테스트넷 연결 확인 ===")
    print(f"계정: {client.account}")
    print()

    markets = client.get_markets()
    print(f"마켓 수: {len(markets)}")
    for m in markets[:3]:
        print(f"  {m['symbol']}: 펀딩비={m.get('funding_rate')}, 최대레버리지={m.get('max_leverage')}x")

    print()
    positions = client.get_positions()
    print(f"열린 포지션: {len(positions)}")

    orders = client.get_orders()
    print(f"미체결 주문: {len(orders)}")

    trades = client.get_trades(10)
    print(f"최근 체결: {len(trades)}건")
    if trades:
        t = trades[0]
        print(f"  최근: {t.get('side')} {t.get('amount')} @ {t.get('price')}")

    print()
    print("✅ 테스트넷 연결 성공")
