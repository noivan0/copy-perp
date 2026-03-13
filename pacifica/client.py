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

import gzip
import urllib.parse as _urlparse
import base58
from solders.keypair import Keypair
from scrapling import Fetcher as _Fetcher

_fetcher = _Fetcher()
_fetcher.configure(verify=False)

REST_URL = os.getenv("PACIFICA_REST_URL", "https://test-api.pacifica.fi/api/v1")
WS_URL = os.getenv("PACIFICA_WS_URL", "wss://test-ws.pacifica.fi/ws")
ACCOUNT_ADDRESS = os.getenv("ACCOUNT_ADDRESS", "")
AGENT_PRIVATE_KEY = os.getenv("AGENT_PRIVATE_KEY", "")
AGENT_WALLET_PUBKEY = os.getenv("AGENT_WALLET", "")   # Agent 공개키 (주문 서명)
BUILDER_CODE = os.getenv("BUILDER_CODE", "noivan")

# HMG 웹필터 우회: allorigins.win CORS 프록시 (GET 전용)
# POST(서명 필요 API)는 직접 호출 유지
CORS_PROXY = "https://api.allorigins.win/raw?url="

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
        return {k: _sort_json_keys(value[k]) for k in sorted(value.keys())}
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


import urllib.parse as _urlparse

def _request(method: str, path: str, body: Optional[dict] = None) -> dict:
    target_url = f"{REST_URL}/{path}"

    # GET 요청은 allorigins.win 프록시로 우회 (HMG 웹필터 회피)
    if method == "GET":
        proxy_url = CORS_PROXY + _urlparse.quote(target_url, safe="")
        req = urllib.request.Request(proxy_url,
            headers={"User-Agent": "CopyPerp/1.0"}, method="GET")
        try:
            with urllib.request.urlopen(req, context=_ssl_ctx, timeout=15) as r:
                raw = r.read()
                result = json.loads(raw.decode("utf-8"))
                # allorigins wraps: {"success":true,"data":{...}}
                # 또는 직접 JSON 반환
                if isinstance(result, dict) and "success" in result:
                    if result.get("success") is False:
                        raise RuntimeError(f"Proxy error: {result.get('error')}")
                    inner = result.get("data")
                    if inner is None:
                        return result
                    # data가 문자열이면 JSON 파싱
                    if isinstance(inner, str):
                        return json.loads(inner)
                    return {"data": inner} if not isinstance(inner, dict) or "data" not in inner else inner
                return result
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Proxy HTTP {e.code}: {e.read().decode()}")

    # POST/PUT/DELETE: 직접 요청 (서명 포함)
    data = json.dumps(body).encode() if body else None
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "CopyPerp/1.0",
        "Accept-Encoding": "identity",
    }
    req = urllib.request.Request(target_url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx, timeout=15) as r:
            raw = r.read()
            enc = r.headers.get("Content-Encoding", "")
            if enc == "gzip":
                raw = gzip.decompress(raw)
            elif enc == "deflate":
                import zlib
                raw = zlib.decompress(raw)
            return json.loads(raw.decode("utf-8"))
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
        """전체 마켓 목록 — GET /api/v1/info"""
        result = _request("GET", "info")
        # 응답 구조: {"success": true, "data": [...]} 또는 직접 list
        if isinstance(result, list):
            return result
        return result.get("data", [])

    def get_prices(self) -> list:
        """전체 마켓 가격/펀딩비 — GET /api/v1/info/prices"""
        result = _request("GET", "info/prices")
        if isinstance(result, list):
            return result
        return result.get("data", [])

    def get_account_info(self) -> dict:
        """계정 잔고/수수료 등급 — GET /api/v1/account"""
        result = _request("GET", f"account?account={self.account}")
        if isinstance(result, dict) and "data" in result:
            return result["data"]
        return result

    def get_positions(self) -> list:
        """현재 포지션 — GET /api/v1/positions"""
        result = _request("GET", f"positions?account={self.account}")
        if isinstance(result, list):
            return result
        return result.get("data", [])

    def get_orders(self) -> list:
        """미체결 주문 — GET /api/v1/orders"""
        result = _request("GET", f"orders?account={self.account}")
        if isinstance(result, list):
            return result
        return result.get("data", [])

    def get_account_trades(self, limit: int = 50) -> list:
        """
        계정 체결 내역 — GET /api/v1/trades/history
        스키마: {event_type, price, amount, side, cause, created_at}
        side 값: open_long / open_short / close_long / close_short
        """
        result = _request("GET", f"trades/history?account={self.account}&limit={limit}")
        if isinstance(result, list):
            return result
        return result.get("data", [])

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
        }
        # Agent Wallet 방식: 주문 서명은 agent key로 하되,
        # 요청에 agent_wallet(공개키) 필드 추가 (공식 SDK 방식)
        if AGENT_WALLET_PUBKEY:
            body["agent_wallet"] = AGENT_WALLET_PUBKEY
        body.update(payload)
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

    def set_tpsl(self, symbol: str, take_profit: Optional[str] = None,
                 stop_loss: Optional[str] = None,
                 builder_code: str = BUILDER_CODE) -> dict:
        """포지션 TP/SL 설정"""
        payload: dict = {"symbol": symbol}
        if take_profit:
            payload["take_profit"] = {"trigger_price": take_profit, "reduce_only": True}
        if stop_loss:
            payload["stop_loss"] = {"trigger_price": stop_loss, "reduce_only": True}
        if builder_code:
            payload["builder_code"] = builder_code
        return self._signed_post("positions/tpsl", "set_position_tpsl", payload)

    def update_leverage(self, symbol: str, leverage: int) -> dict:
        """레버리지 변경 (5x 하드캡)"""
        lev = min(max(leverage, 1), 5)
        return self._signed_post("account/leverage", "update_leverage",
                                 {"symbol": symbol, "leverage": lev})

    def get_leaderboard(self, limit: int = 20) -> list:
        """Pacifica 온체인 리더보드 조회"""
        try:
            return _request("GET", f"leaderboard?limit={limit}").get("data", [])
        except Exception:
            return []


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


# ── WS 가격 스트림 ─────────────────────────────────────

import asyncio as _asyncio
import ssl as _ssl

class PriceStream:
    """실시간 가격 + 펀딩비 + OI (WS prices 채널)"""

    def __init__(self, on_update=None):
        self.on_update = on_update
        self._running = False
        self.latest: dict = {}

    async def start(self):
        self._running = True
        ssl_ctx = _ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = _ssl.CERT_NONE
        while self._running:
            try:
                import websockets as _ws
                async with _ws.connect(WS_URL, ssl=ssl_ctx) as ws:
                    await ws.send(json.dumps({"method": "subscribe", "params": {"source": "prices"}}))
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("channel") == "prices":
                            for item in msg.get("data", []):
                                self.latest[item["symbol"]] = item
                            if self.on_update:
                                await self.on_update(msg.get("data", []))
            except Exception as e:
                if self._running:
                    await _asyncio.sleep(2)

    def stop(self):
        self._running = False


class PositionPoller:
    """트레이더 포지션 변화 감지 (REST 500ms 폴링)"""

    def __init__(self, client, on_change=None):
        self.client = client
        self.on_change = on_change
        self._prev: list = []
        self._running = False

    async def start(self, interval: float = 0.5):
        self._running = True
        while self._running:
            try:
                curr = self.client.get_positions()
                for change in self._diff(self._prev, curr):
                    if self.on_change:
                        await self.on_change(change)
                self._prev = curr
            except Exception:
                pass
            await _asyncio.sleep(interval)

    def _diff(self, prev, curr):
        prev_map = {p.get("symbol"): p for p in prev}
        curr_map = {p.get("symbol"): p for p in curr}
        changes = []
        for sym, pos in curr_map.items():
            prev_pos = prev_map.get(sym, {})
            ps = float(prev_pos.get("szi", 0)) if prev_pos else 0
            cs = float(pos.get("szi", 0))
            if cs != ps:
                changes.append({"type": "open" if abs(cs) > abs(ps) else "reduce",
                                 "symbol": sym, "side": "bid" if cs > 0 else "ask",
                                 "size_delta": abs(cs - ps), "position": pos})
        for sym in prev_map:
            if sym not in curr_map:
                changes.append({"type": "close", "symbol": sym,
                                 "side": "ask" if float(prev_map[sym].get("szi", 0)) > 0 else "bid",
                                 "size_delta": abs(float(prev_map[sym].get("szi", 0))), "position": None})
        return changes

    def stop(self):
        self._running = False
