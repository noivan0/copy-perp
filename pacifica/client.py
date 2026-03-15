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
import socket
import urllib.request
import urllib.error
from typing import Optional

import gzip
import urllib.parse as _urlparse
import base58
from solders.keypair import Keypair
from scrapling import Fetcher as _Fetcher

_fetcher = _Fetcher()

NETWORK = os.getenv("NETWORK", "testnet")
REST_URL = os.getenv("PACIFICA_REST_URL", "https://do5jt23sqak4.cloudfront.net/api/v1")
# GET 프록시용 실제 URL — NETWORK 환경변수로 mainnet/testnet 자동 선택
PACIFICA_REST_URL_DIRECT = (
    "https://api.pacifica.fi/api/v1" if NETWORK == "mainnet"
    else "https://test-api.pacifica.fi/api/v1"
)
WS_URL = os.getenv("PACIFICA_WS_URL", "wss://test-ws.pacifica.fi/ws")
ACCOUNT_ADDRESS = os.getenv("ACCOUNT_ADDRESS", "")
AGENT_PRIVATE_KEY = os.getenv("AGENT_PRIVATE_KEY", "")
AGENT_WALLET_PUBKEY = os.getenv("AGENT_WALLET", "")   # Agent 공개키 (주문 서명)
BUILDER_CODE = os.getenv("BUILDER_CODE", "noivan")

# HMG 웹필터 우회:
#   testnet: CloudFront 도메인(do5jt23sqak4.cloudfront.net) SNI + Host: test-api.pacifica.fi
#   mainnet: IP 54.230.62.105 직접 접근 + Host: api.pacifica.fi (HMG 통과 확인됨)
CORS_PROXY = "https://api.allorigins.win/raw?url="
# CF 도메인 = testnet 접근용 (do5jt23sqak4.cloudfront.net)
_CF_HOST = os.getenv("PACIFICA_CF_HOST", "do5jt23sqak4.cloudfront.net")
# CloudFront origin Host 헤더 — PACIFICA_HOST 환경변수 우선, fallback은 NETWORK 기반 자동 선택
_PACIFICA_HOST = os.getenv("PACIFICA_HOST", "api.pacifica.fi" if NETWORK == "mainnet" else "test-api.pacifica.fi")
CF_SNI_HOST = _CF_HOST  # SNI = CloudFront 도메인 (HMG 필터 통과)

# Mainnet 직접 접근 설정 (IP 직접 + Host 헤더)
MAINNET_IP = os.getenv("PACIFICA_MAINNET_IP", "54.230.62.105")
MAINNET_HOST = "api.pacifica.fi"

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def _load_keypair() -> Optional[Keypair]:
    # 모듈 로드 시점에 env가 비어있을 수 있으므로 매번 최신 env 재참조
    pk = os.getenv("AGENT_PRIVATE_KEY", "") or AGENT_PRIVATE_KEY
    if not pk:
        return None
    try:
        seed = base58.b58decode(pk)
        return Keypair.from_seed(seed[:32])
    except Exception:
        return Keypair.from_base58_string(pk)


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


def _parse_json(text: str):
    """텍스트에서 첫 번째 JSON 오브젝트/배열 파싱"""
    for ch in ('{', '['):
        idx = text.find(ch)
        if idx >= 0:
            return json.loads(text[idx:])
    raise RuntimeError(f"JSON 없음: {text[:100]}")


def _unwrap(result):
    """allorigins 래핑 처리: {success, data} → data (dict or list 모두 처리)"""
    if isinstance(result, dict) and "success" in result:
        if result.get("success") is False:
            raise RuntimeError(f"Proxy error: {result.get('error')}")
        inner = result.get("data")
        if inner is None:
            return result
        if isinstance(inner, str):
            return json.loads(inner)
        # list면 그대로, dict면 그대로 반환
        return inner
    return result


def _proxy_get(path: str) -> dict:
    """GET 요청 — HMG 웹필터 우회 (allorigins → codetabs fallback)"""
    target_url = f"{PACIFICA_REST_URL_DIRECT}/{path}"

    # 1차: allorigins.win (파라미터 없는 URL에 강함)
    try:
        proxy_url = CORS_PROXY + _urlparse.quote(target_url, safe="")
        page = _fetcher.get(proxy_url, timeout=15)
        text = page.get_all_text()
        # 500/520 오류면 codetabs로 폴백
        if text and not any(e in text[:50] for e in ("500", "520", "Error", "nginx")):
            result = _unwrap(_parse_json(text))
            # 유효한 결과만 반환 (빈 dict/list도 OK, None은 폴백)
            if result is not None:
                return result
    except Exception:
        pass

    # 2차: codetabs.com (쿼리파라미터 포함 URL에 강함)
    try:
        codetabs_url = f"https://api.codetabs.com/v1/proxy/?quest={target_url}"
        page2 = _fetcher.get(codetabs_url, timeout=15)
        text2 = page2.get_all_text()
        if len(text2) > 10:
            return _parse_json(text2)
    except Exception as e:
        raise RuntimeError(f"모든 프록시 실패: {e}")


POST_PROXY = "https://cors.bridged.cc/"  # POST 지원 CORS 프록시


def _cf_request(method: str, path: str, body: Optional[dict] = None) -> dict:
    """GET/POST — CloudFront 도메인으로 HMG 웹필터 우회
    
    CloudFront 도메인(do5jt23sqak4.cloudfront.net)은 HMG 필터 통과
    Host 헤더에 test-api.pacifica.fi 지정 → CloudFront가 Pacifica로 라우팅
    """
    # URL 경로 파싱
    url_path = f"/api/v1/{path}"
    body_bytes = json.dumps(body).encode() if body else None

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    s = socket.create_connection((_CF_HOST, 443), timeout=15)
    ss = ctx.wrap_socket(s, server_hostname=_CF_HOST)

    headers = (
        f"{method} {url_path} HTTP/1.1\r\n"
        f"Host: {_PACIFICA_HOST}\r\n"
        f"Content-Type: application/json\r\n"
        f"Accept: application/json\r\n"
        f"User-Agent: CopyPerp/1.0\r\n"
    )
    if body_bytes:
        headers += f"Content-Length: {len(body_bytes)}\r\n"
    headers += "Connection: close\r\n\r\n"

    ss.sendall(headers.encode() + (body_bytes or b""))

    data = b""
    ss.settimeout(15)
    try:
        while True:
            chunk = ss.recv(8192)
            if not chunk:
                break
            data += chunk
    except Exception:
        pass

    if not data:
        raise RuntimeError("빈 응답 — 연결 실패")

    status_line = data.split(b"\r\n")[0].decode("utf-8", "ignore")
    status_code = int(status_line.split()[1]) if len(status_line.split()) > 1 else 0
    blocked = b"secinfo.hmg" in data or b"buychal" in data
    if blocked:
        raise RuntimeError("HMG 웹필터 차단")

    raw_body = data.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in data else b""
    hdrs_raw = data.split(b"\r\n\r\n", 1)[0].decode("utf-8", "ignore")

    # gzip 디코딩
    for line in hdrs_raw.split("\r\n"):
        if "content-encoding" in line.lower() and "gzip" in line.lower():
            raw_body = gzip.decompress(raw_body)
            break

    body_text = raw_body.decode("utf-8", "ignore").strip()
    if not body_text:
        if status_code >= 400:
            raise RuntimeError(f"HTTP {status_code}: (empty body)")
        return {}
    try:
        result = json.loads(body_text)
    except json.JSONDecodeError:
        if status_code >= 400:
            raise RuntimeError(f"HTTP {status_code}: {body_text[:300]}")
        return {"raw": body_text}

    if status_code >= 400:
        err = result.get("error", body_text) if isinstance(result, dict) else body_text
        raise RuntimeError(f"HTTP {status_code}: {err}")

    return result


def _mainnet_request(method: str, path: str, body: Optional[dict] = None) -> dict:
    """Mainnet 직접 접근 — IP 54.230.62.105 + Host: api.pacifica.fi (HMG 통과)"""
    url_path = f"/api/v1/{path}"
    body_bytes = json.dumps(body).encode() if body else None

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    raw = socket.create_connection((MAINNET_IP, 443), timeout=15)
    s = ctx.wrap_socket(raw, server_hostname=MAINNET_HOST)

    headers = (
        f"{method} {url_path} HTTP/1.1\r\n"
        f"Host: {MAINNET_HOST}\r\n"
        f"Content-Type: application/json\r\n"
        f"Accept: application/json\r\n"
        f"Accept-Encoding: identity\r\n"
        f"User-Agent: CopyPerp/1.0\r\n"
    )
    if body_bytes:
        headers += f"Content-Length: {len(body_bytes)}\r\n"
    headers += "Connection: close\r\n\r\n"

    s.sendall(headers.encode() + (body_bytes or b""))

    data = b""
    s.settimeout(15)
    try:
        while True:
            chunk = s.recv(8192)
            if not chunk:
                break
            data += chunk
    except Exception:
        pass

    if not data:
        raise RuntimeError("Mainnet 빈 응답 — 연결 실패")

    status_line = data.split(b"\r\n")[0].decode("utf-8", "ignore")
    status_code = int(status_line.split()[1]) if len(status_line.split()) > 1 else 0

    blocked = b"secinfo.hmg" in data or b"buychal" in data
    if blocked:
        raise RuntimeError("HMG 웹필터 차단 (mainnet)")

    raw_body = data.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in data else b""
    hdrs_raw = data.split(b"\r\n\r\n", 1)[0].decode("utf-8", "ignore")

    # gzip 디코딩
    for line in hdrs_raw.split("\r\n"):
        if "content-encoding" in line.lower() and "gzip" in line.lower():
            raw_body = gzip.decompress(raw_body)
            break

    result = json.loads(raw_body.decode("utf-8", "ignore"))

    if status_code >= 400:
        raise RuntimeError(f"HTTP {status_code}: {json.dumps(result)[:300]}")

    return result


def _mainnet_proxy_get(path: str) -> dict:
    """Mainnet GET — codetabs 프록시 (allorigins보다 안정적)"""
    import urllib.parse as _up
    target = f"https://api.pacifica.fi/api/v1/{path.lstrip('/')}"
    url = f"https://api.codetabs.com/v1/proxy/?quest={_up.quote(target, safe='')}"
    req = urllib.request.Request(url, headers={"User-Agent": "CopyPerp/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read())
        return d.get("data", d) if isinstance(d, dict) and "data" in d else d


def _request(method: str, path: str, body: Optional[dict] = None) -> dict:
    """NETWORK 환경변수 기반 자동 라우팅:
    - mainnet GET: codetabs 프록시 (직접 접근 확인됨)
    - mainnet POST: IP 직접 접근 (54.230.62.105) + Host 헤더
    - testnet GET: CloudFront SNI → scrapling 프록시 fallback
    - testnet POST: CloudFront SNI 우회
    """
    if NETWORK == "mainnet":
        if method == "GET":
            try:
                return _mainnet_proxy_get(path)
            except Exception:
                return _proxy_get(path)  # allorigins fallback
        else:
            # POST: IP 직접 + 실패 시 CF SNI(testnet 우회 경유)
            try:
                return _mainnet_request(method, path, body)
            except Exception:
                return _cf_request(method, path, body)

    # testnet: GET는 CloudFront SNI 1차, scrapling 프록시 fallback
    if method == "GET":
        try:
            return _cf_request("GET", path)
        except Exception:
            return _proxy_get(path)

    # POST/PUT/DELETE: CloudFront SNI 우회
    return _cf_request(method, path, body)


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
        """
        공식 SDK sign_message 방식:
          서명 대상: {timestamp, expiry_window, type, data: payload}  (재귀 정렬 → compact JSON)
          요청 body: {account, agent_wallet, signature, timestamp, expiry_window, **payload}  (data 래퍼 제거, top-level flatten)

        중요: builder_code는 payload(data) 안에 포함되어야 서명 대상에 들어감.
             top-level에만 있으면 서명 검증 실패.
        """
        if not self._kp:
            raise RuntimeError("AGENT_PRIVATE_KEY 미설정 — 주문 실행 불가")
        timestamp = int(time.time() * 1000)
        header = {"timestamp": timestamp, "expiry_window": 5000, "type": order_type}
        # payload에 builder_code가 있으면 서명 대상(data)에도 포함됨 — 공식 문서 기준
        _, signature = _sign_request(header, payload, self._kp)
        body = {
            "account": self.account,
            "agent_wallet": AGENT_WALLET_PUBKEY or None,
            "signature": signature,
            "timestamp": timestamp,
            "expiry_window": 5000,
        }
        # payload를 top-level로 flatten (data 래퍼 제거)
        body.update(payload)
        return _request("POST", endpoint_path, body)

    def market_order(
        self,
        symbol: str,
        side: str,           # "bid" (롱) / "ask" (숏)
        amount: str,
        slippage_percent: str = "0.5",
        builder_code: Optional[str] = None,  # 승인된 경우 BUILDER_CODE 전달
        client_order_id: Optional[str] = None,
    ) -> dict:
        """
        시장가 주문 생성.

        공식 문서 서명 구조 (builder_code 포함 시):
          data = {
            "symbol": ..., "amount": ..., "side": ...,
            "slippage_percent": ..., "reduce_only": false,
            "client_order_id": ...,
            "builder_code": "noivan"   ← data 안에 포함해야 서명 검증 통과
          }
        """
        payload = {
            "symbol": symbol,
            "side": side,
            "amount": amount,
            "reduce_only": False,
            "slippage_percent": slippage_percent,
            "client_order_id": client_order_id or str(uuid.uuid4()),
        }
        # builder_code는 data(payload) 안에 포함 — 공식 문서 기준
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

    def get_leaderboard(self, limit: int = 10) -> list:
        """Pacifica 온체인 리더보드 조회 (limit: 10, 100, 25000만 허용)
        
        NETWORK 분기:
        - mainnet: /api/v1/leaderboard (info/ prefix 없음) — codetabs 프록시 경유
        - testnet: /api/v1/leaderboard (동일 엔드포인트, CF SNI 경유)
        
        주의: testnet은 /info/leaderboard 아님 — 두 환경 모두 /leaderboard 사용
        """
        # Pacifica API는 10, 100, 25000만 허용
        allowed = [10, 100, 25000]
        api_limit = min((x for x in allowed if x >= limit), default=100)
        try:
            if NETWORK == "mainnet":
                # mainnet: codetabs 프록시 우선 (직접 접근 검증됨)
                try:
                    result = _mainnet_proxy_get(f"leaderboard?limit={api_limit}")
                except Exception:
                    result = _proxy_get(f"leaderboard?limit={api_limit}")
            else:
                # testnet: CloudFront SNI → scrapling 프록시 fallback
                result = _request("GET", f"leaderboard?limit={api_limit}")

            if isinstance(result, list):
                return result[:limit]
            if isinstance(result, dict):
                return result.get("data", [])[:limit]
            return []
        except Exception:
            return []


def approve_builder_code(
    main_private_key: str,
    account_address: str,
    builder_code: str = "noivan",
    max_fee_rate: str = "0.001",
) -> dict:
    """
    Builder Code approve — **main account private key**로 서명 필요.
    Agent wallet이 아닌 main account 키로 직접 서명.

    공식 문서 서명 구조:
        서명 대상 =
        {
          "timestamp": <ms>,
          "expiry_window": 5000,
          "type": "approve_builder_code",
          "data": {
            "builder_code": "noivan",
            "max_fee_rate": "0.001"
          }
        }
        → 재귀 정렬 → compact JSON → Ed25519 sign → base58

        요청 body =
        {
          "account": "<address>",
          "agent_wallet": null,
          "signature": "<base58>",
          "timestamp": <ms>,
          "expiry_window": 5000,
          "builder_code": "noivan",     ← data 래퍼 제거, top-level
          "max_fee_rate": "0.001"       ← data 래퍼 제거, top-level
        }

    Returns:
        API 응답 dict (success=True이면 승인 완료)
    """
    try:
        seed = base58.b58decode(main_private_key)
        kp = Keypair.from_seed(seed[:32])
    except Exception:
        kp = Keypair.from_base58_string(main_private_key)

    ts = int(time.time() * 1000)

    # 서명 대상: header + data 구조 (공식 sign_message 방식)
    sign_header = {
        "timestamp": ts,
        "expiry_window": 5000,
        "type": "approve_builder_code",
    }
    sign_data = {
        "builder_code": builder_code,
        "max_fee_rate": max_fee_rate,
    }
    _, sig_b58 = _sign_request(sign_header, sign_data, kp)

    # 요청 body: data 래퍼 제거, top-level flatten
    request_body = {
        "account": account_address,
        "agent_wallet": None,
        "signature": sig_b58,
        "timestamp": ts,
        "expiry_window": 5000,
        "builder_code": builder_code,
        "max_fee_rate": max_fee_rate,
    }
    return _cf_request("POST", "account/builder_codes/approve", request_body)


def check_builder_approvals(account_address: str) -> list:
    """승인된 builder code 목록 조회"""
    result = _request("GET", f"account/builder_codes/approvals?account={account_address}")
    if isinstance(result, dict):
        return result.get("data", [])
    return result or []


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
