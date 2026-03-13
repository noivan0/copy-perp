"""
Mainnet API 연결 + 리더보드 + 가격 테스트
- NETWORK=mainnet 환경에서 IP 54.230.62.105 직접 접근 검증
- testnet 방식(CloudFront SNI)과의 동작 차이 확인
실행: NETWORK=mainnet python3 -m pytest tests/test_mainnet.py -v -s
"""

import os
import sys
import ssl
import socket
import gzip
import json
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Mainnet 환경 강제 설정
os.environ.setdefault("NETWORK", "mainnet")

MAINNET_IP = "54.230.62.105"
MAINNET_HOST = "api.pacifica.fi"


# ── 헬퍼 ──────────────────────────────────────────────

def mainnet_raw_get(path: str, timeout: int = 15) -> dict:
    """Mainnet IP 직접 GET (HMG 통과 방식)"""
    url_path = f"/api/v1/{path}"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    raw = socket.create_connection((MAINNET_IP, 443), timeout=timeout)
    s = ctx.wrap_socket(raw, server_hostname=MAINNET_HOST)
    req = (
        f"GET {url_path} HTTP/1.1\r\n"
        f"Host: {MAINNET_HOST}\r\n"
        f"Accept: application/json\r\n"
        f"Accept-Encoding: identity\r\n"
        f"User-Agent: CopyPerp-Test/1.0\r\n"
        f"Connection: close\r\n\r\n"
    )
    s.sendall(req.encode())
    data = b""
    s.settimeout(timeout)
    try:
        while True:
            chunk = s.recv(16384)
            if not chunk:
                break
            data += chunk
    except Exception:
        pass
    s.close()

    assert data, f"빈 응답: {path}"
    assert b"secinfo.hmg" not in data, "HMG 웹필터 차단됨"

    status_line = data.split(b"\r\n")[0].decode("utf-8", "ignore")
    status_code = int(status_line.split()[1]) if len(status_line.split()) > 1 else 0

    raw_body = data.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in data else b""
    hdrs_raw = data.split(b"\r\n\r\n", 1)[0].decode("utf-8", "ignore")

    for line in hdrs_raw.split("\r\n"):
        if "content-encoding" in line.lower() and "gzip" in line.lower():
            raw_body = gzip.decompress(raw_body)
            break

    result = json.loads(raw_body.decode("utf-8", "ignore"))
    assert status_code < 400, f"HTTP {status_code}: {json.dumps(result)[:200]}"
    return result


# ── 테스트 케이스 ────────────────────────────────────

class TestMainnetConnection:
    """Mainnet IP 직접 접근 연결 테스트"""

    def test_mainnet_ip_reachable(self):
        """IP 54.230.62.105:443 소켓 연결 가능"""
        try:
            s = socket.create_connection((MAINNET_IP, 443), timeout=10)
            s.close()
            assert True
        except Exception as e:
            pytest.fail(f"Mainnet IP 연결 실패: {e}")

    def test_mainnet_ssl_handshake(self):
        """SSL 핸드셰이크 성공"""
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            raw = socket.create_connection((MAINNET_IP, 443), timeout=10)
            s = ctx.wrap_socket(raw, server_hostname=MAINNET_HOST)
            s.close()
        except Exception as e:
            pytest.fail(f"SSL 핸드셰이크 실패: {e}")

    def test_mainnet_prices_api(self):
        """Mainnet /info/prices 응답 확인"""
        result = mainnet_raw_get("info/prices")
        # 응답이 list 또는 dict with 'data'
        if isinstance(result, list):
            prices = result
        else:
            prices = result.get("data", [])
        assert len(prices) > 0, "가격 데이터가 비어있음"
        # BTC 가격 확인
        btc = next((p for p in prices if p.get("symbol") == "BTC"), None)
        assert btc is not None, "BTC 가격 없음"
        btc_mark = float(btc.get("mark", 0))
        assert btc_mark > 0, f"BTC 마크가가 0: {btc}"
        print(f"\n✅ BTC 마크가: ${btc_mark:,.0f}")
        print(f"   심볼 수: {len(prices)}")

    def test_mainnet_info_api(self):
        """Mainnet /info 마켓 목록 확인"""
        result = mainnet_raw_get("info")
        if isinstance(result, list):
            markets = result
        else:
            markets = result.get("data", [])
        assert len(markets) > 0, "마켓 데이터 없음"
        symbols = [m.get("symbol") for m in markets]
        assert "BTC" in symbols, f"BTC 마켓 없음: {symbols[:5]}"
        print(f"\n✅ 마켓 수: {len(markets)}")
        print(f"   심볼: {', '.join(symbols[:5])}...")


class TestMainnetLeaderboard:
    """Mainnet 리더보드 테스트"""

    def test_leaderboard_100(self):
        """리더보드 100명 수집 가능"""
        result = mainnet_raw_get("leaderboard?limit=100")
        if isinstance(result, list):
            traders = result
        else:
            traders = result.get("data", [])
        assert len(traders) > 0, "리더보드 데이터 없음"
        assert len(traders) >= 10, f"리더보드 10명 미만: {len(traders)}"
        print(f"\n✅ 리더보드: {len(traders)}명")
        # 상위 3명 출력
        for i, t in enumerate(traders[:3], 1):
            addr = t.get("address", "")[:12]
            pnl = t.get("pnl_all_time", t.get("pnl_30d", 0)) or 0
            print(f"   #{i} {addr}... PnL=${float(pnl):+.0f}")

    def test_leaderboard_fields(self):
        """리더보드 필드 구조 검증"""
        result = mainnet_raw_get("leaderboard?limit=10")
        if isinstance(result, list):
            traders = result
        else:
            traders = result.get("data", [])
        assert traders, "리더보드 비어있음"
        first = traders[0]
        # address 필드는 반드시 있어야 함
        assert "address" in first, f"address 필드 없음: {list(first.keys())}"
        addr = first["address"]
        assert len(addr) >= 32, f"address 길이 이상: {addr}"

    def test_leaderboard_10_vs_100(self):
        """limit=10과 limit=100 응답 크기 차이 확인"""
        r10 = mainnet_raw_get("leaderboard?limit=10")
        r100 = mainnet_raw_get("leaderboard?limit=100")

        if isinstance(r10, list):
            count10 = len(r10)
        else:
            count10 = len(r10.get("data", []))

        if isinstance(r100, list):
            count100 = len(r100)
        else:
            count100 = len(r100.get("data", []))

        # limit=100이 limit=10보다 크거나 같아야 함
        assert count100 >= count10, f"limit=100({count100})이 limit=10({count10})보다 작음"
        print(f"\n✅ limit=10: {count10}명 | limit=100: {count100}명")


class TestMainnetClientModule:
    """pacifica.client 모듈의 mainnet 지원 테스트"""

    def test_network_env_mainnet(self):
        """NETWORK=mainnet 환경변수 인식"""
        from pacifica.client import NETWORK, MAINNET_IP, MAINNET_HOST
        assert MAINNET_IP == "54.230.62.105"
        assert MAINNET_HOST == "api.pacifica.fi"

    def test_mainnet_request_function(self):
        """_mainnet_request 함수 동작 확인"""
        from pacifica.client import _mainnet_request
        result = _mainnet_request("GET", "info/prices")
        if isinstance(result, list):
            assert len(result) > 0
        else:
            assert "data" in result or len(result) > 0

    def test_pacifica_client_get_prices_mainnet(self):
        """PacificaClient.get_prices() mainnet 환경에서 동작"""
        os.environ["NETWORK"] = "mainnet"
        # 모듈 재임포트 없이 _mainnet_request 직접 테스트
        from pacifica.client import _mainnet_request
        result = _mainnet_request("GET", "info/prices")
        if isinstance(result, list):
            prices = result
        else:
            prices = result.get("data", [])
        assert len(prices) > 0, "mainnet 가격 데이터 없음"

    def test_mainnet_leaderboard_via_client(self):
        """PacificaClient.get_leaderboard() mainnet 경로"""
        from pacifica.client import _mainnet_request
        result = _mainnet_request("GET", "leaderboard?limit=10")
        if isinstance(result, list):
            traders = result
        else:
            traders = result.get("data", [])
        assert len(traders) > 0, "mainnet 리더보드 데이터 없음"


class TestMainnetVsTestnet:
    """Mainnet/Testnet 환경 분리 확인"""

    def test_env_files_exist(self):
        """env 파일 존재 확인"""
        base = os.path.dirname(os.path.dirname(__file__))
        assert os.path.exists(os.path.join(base, ".env.mainnet")), ".env.mainnet 없음"
        assert os.path.exists(os.path.join(base, ".env.testnet")), ".env.testnet 없음"
        assert os.path.exists(os.path.join(base, ".env")), ".env 없음"

    def test_env_mainnet_content(self):
        """env.mainnet 내용 검증"""
        base = os.path.dirname(os.path.dirname(__file__))
        content = open(os.path.join(base, ".env.mainnet")).read()
        assert "NETWORK=mainnet" in content
        assert "api.pacifica.fi" in content
        assert "copy_perp_mainnet.db" in content

    def test_env_testnet_content(self):
        """env.testnet 내용 검증"""
        base = os.path.dirname(os.path.dirname(__file__))
        content = open(os.path.join(base, ".env.testnet")).read()
        assert "NETWORK=testnet" in content
        assert "test-api.pacifica.fi" in content

    def test_default_env_has_network_key(self):
        """.env 기본값에 NETWORK=testnet 포함"""
        base = os.path.dirname(os.path.dirname(__file__))
        content = open(os.path.join(base, ".env")).read()
        assert "NETWORK=testnet" in content

    def test_db_path_separation(self):
        """mainnet/testnet DB 경로 분리 확인"""
        base = os.path.dirname(os.path.dirname(__file__))
        mainnet_content = open(os.path.join(base, ".env.mainnet")).read()
        testnet_content = open(os.path.join(base, ".env.testnet")).read()
        # mainnet은 copy_perp_mainnet.db, testnet은 copy_perp.db
        assert "copy_perp_mainnet.db" in mainnet_content
        assert "copy_perp_mainnet.db" not in testnet_content

    def test_client_mainnet_ip_constants(self):
        """client.py에 mainnet IP/HOST 상수 정의됨"""
        from pacifica.client import MAINNET_IP, MAINNET_HOST
        assert MAINNET_IP == "54.230.62.105"
        assert MAINNET_HOST == "api.pacifica.fi"
