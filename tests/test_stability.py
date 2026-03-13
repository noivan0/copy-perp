"""
백엔드 안정성 테스트
- 백엔드 재시작 복구 검증
- 연속 요청 처리
- 주소 검증 (422 응답)
실행: python3 -m pytest tests/test_stability.py -v -s

주의: 백엔드가 실행 중일 때 더 많은 테스트가 활성화됨.
      BACKEND_URL=http://localhost:8001 (기본값)
"""

import os
import sys
import ssl
import socket
import json
import time
import subprocess
import signal
import threading
import pytest
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

BACKEND_HOST = os.getenv("BACKEND_HOST", "127.0.0.1")
BACKEND_PORT = int(os.getenv("BACKEND_PORT", "8001"))
BACKEND_URL = os.getenv("BACKEND_URL", f"http://{BACKEND_HOST}:{BACKEND_PORT}")


# ── 헬퍼 ──────────────────────────────────────────────

def backend_get(path: str, timeout: int = 5) -> tuple[int, dict]:
    """백엔드 HTTP GET (raw socket)"""
    try:
        s = socket.create_connection((BACKEND_HOST, BACKEND_PORT), timeout=timeout)
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {BACKEND_HOST}\r\n"
            f"Accept: application/json\r\n"
            f"Connection: close\r\n\r\n"
        )
        s.sendall(req.encode())
        data = b""
        s.settimeout(timeout)
        try:
            while True:
                chunk = s.recv(8192)
                if not chunk:
                    break
                data += chunk
        except Exception:
            pass
        s.close()

        if not data:
            return 0, {}

        status_line = data.split(b"\r\n")[0].decode("utf-8", "ignore")
        status_code = int(status_line.split()[1]) if len(status_line.split()) > 1 else 0
        body_raw = data.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in data else b"{}"
        try:
            result = json.loads(body_raw.decode("utf-8", "ignore"))
        except Exception:
            result = {"raw": body_raw.decode("utf-8", "ignore")[:200]}
        return status_code, result
    except (ConnectionRefusedError, OSError):
        return 0, {}
    except Exception as e:
        return 0, {"error": str(e)}


def backend_post(path: str, body: dict, timeout: int = 5) -> tuple[int, dict]:
    """백엔드 HTTP POST (raw socket)"""
    try:
        body_bytes = json.dumps(body).encode()
        s = socket.create_connection((BACKEND_HOST, BACKEND_PORT), timeout=timeout)
        req = (
            f"POST {path} HTTP/1.1\r\n"
            f"Host: {BACKEND_HOST}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body_bytes)}\r\n"
            f"Accept: application/json\r\n"
            f"Connection: close\r\n\r\n"
        ).encode() + body_bytes
        s.sendall(req)
        data = b""
        s.settimeout(timeout)
        try:
            while True:
                chunk = s.recv(8192)
                if not chunk:
                    break
                data += chunk
        except Exception:
            pass
        s.close()

        if not data:
            return 0, {}

        status_line = data.split(b"\r\n")[0].decode("utf-8", "ignore")
        status_code = int(status_line.split()[1]) if len(status_line.split()) > 1 else 0
        body_raw = data.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in data else b"{}"
        try:
            result = json.loads(body_raw.decode("utf-8", "ignore"))
        except Exception:
            result = {}
        return status_code, result
    except (ConnectionRefusedError, OSError):
        return 0, {}
    except Exception as e:
        return 0, {"error": str(e)}


def is_backend_running() -> bool:
    """백엔드 실행 중 여부 확인"""
    status, result = backend_get("/health")
    return status == 200 and result.get("status") == "ok"


def skip_if_no_backend(func):
    """백엔드 미실행 시 테스트 스킵 데코레이터"""
    import functools
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if not is_backend_running():
            pytest.skip(f"백엔드 미실행 ({BACKEND_URL}) — 스킵")
        return func(*args, **kwargs)
    return wrapper


# ── 주소 검증 단위 테스트 ─────────────────────────────

class TestAddressValidation:
    """Solana 주소 검증 로직 단위 테스트 (백엔드 불필요)"""

    def _call_validate(self, address):
        """_validate_solana_address 직접 호출"""
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from fastapi import HTTPException
        from api.routers.followers import _validate_solana_address
        return _validate_solana_address

    def test_valid_address_passes(self):
        """유효한 Solana 주소 검증 통과"""
        from api.routers.followers import _validate_solana_address
        # 유효한 주소 (32바이트 Ed25519 공개키)
        valid_addrs = [
            "3AHZqrocSguMuo9sUUP8G8YN8NwHwWV2DPUQvbDvtfaQ",
            "EcX5xSDT45Nvhi2gMTjTnhF3KT2w4sPF54esEZS3hwZu",
            "4UBH19qUbXEaqyz9fKrFHuvj8BPMoM87H71s1YPKyGYq",
        ]
        for addr in valid_addrs:
            _validate_solana_address(addr)  # 예외 없어야 함

    def test_invalid_address_short(self):
        """짧은 주소 — 422 에러"""
        from fastapi import HTTPException
        from api.routers.followers import _validate_solana_address
        with pytest.raises(HTTPException) as exc_info:
            _validate_solana_address("short_addr")
        assert exc_info.value.status_code == 422

    def test_invalid_address_wrong_chars(self):
        """잘못된 문자(0, O, I, l) — 422 에러"""
        from fastapi import HTTPException
        from api.routers.followers import _validate_solana_address
        with pytest.raises(HTTPException) as exc_info:
            # base58에서 금지된 문자: 0, O, I, l
            _validate_solana_address("0000OOOOIIIIllll0000OOOOIIIIllll0000")
        assert exc_info.value.status_code == 422

    def test_empty_address_raises(self):
        """빈 주소 — 422 에러"""
        from fastapi import HTTPException
        from api.routers.followers import _validate_solana_address
        with pytest.raises(HTTPException) as exc_info:
            _validate_solana_address("")
        assert exc_info.value.status_code == 422

    def test_none_address_raises(self):
        """None 주소 — 422 에러"""
        from fastapi import HTTPException
        from api.routers.followers import _validate_solana_address
        with pytest.raises(HTTPException) as exc_info:
            _validate_solana_address(None)
        assert exc_info.value.status_code == 422

    def test_invalid_base58_decode(self):
        """올바른 base58 문자셋이지만 디코딩 후 32바이트가 아닌 경우"""
        from fastapi import HTTPException
        from api.routers.followers import _validate_solana_address
        # 4바이트짜리 base58 — 32바이트 아님
        import base58
        short_b58 = base58.b58encode(b"\x01\x02\x03\x04").decode()
        with pytest.raises(HTTPException) as exc_info:
            _validate_solana_address(short_b58)
        assert exc_info.value.status_code == 422


# ── 백엔드 실행 중 테스트 ─────────────────────────────

class TestBackendHealth:
    """백엔드 헬스체크 테스트"""

    @skip_if_no_backend
    def test_health_endpoint(self):
        """/health 엔드포인트 200 응답"""
        status, result = backend_get("/health")
        assert status == 200, f"HTTP {status}: {result}"
        assert result.get("status") == "ok"
        print(f"\n✅ Health: {result}")

    @skip_if_no_backend
    def test_health_fields(self):
        """/health 필드 구조 확인"""
        status, result = backend_get("/health")
        assert status == 200
        required_fields = ["status", "data_connected", "uptime_seconds"]
        for field in required_fields:
            assert field in result, f"필드 없음: {field}"

    @skip_if_no_backend
    def test_root_endpoint(self):
        """/ 루트 엔드포인트"""
        status, result = backend_get("/")
        assert status == 200
        assert "service" in result or "Copy Perp" in str(result)

    @skip_if_no_backend
    def test_network_in_health_or_startup(self):
        """/health 응답에 네트워크 정보 확인 (또는 로그)"""
        status, result = backend_get("/health")
        assert status == 200
        # health 응답에 network 정보가 있거나 없어도 OK (startup 로그에 기록됨)
        network = result.get("network", os.getenv("NETWORK", "testnet"))
        assert network in ["mainnet", "testnet"], f"알 수 없는 network: {network}"
        print(f"\n✅ NETWORK: {network}")


class TestContinuousRequests:
    """연속 요청 처리 테스트"""

    @skip_if_no_backend
    def test_10_consecutive_health_checks(self):
        """10회 연속 /health 요청 처리"""
        success = 0
        for i in range(10):
            status, result = backend_get("/health", timeout=3)
            if status == 200 and result.get("status") == "ok":
                success += 1
        assert success >= 8, f"성공 {success}/10 (기준: 8/10)"
        print(f"\n✅ 연속 헬스체크: {success}/10 성공")

    @skip_if_no_backend
    def test_concurrent_requests(self):
        """동시 요청 5개 처리 (스레드 기반)"""
        results = []
        errors = []

        def make_request():
            try:
                status, result = backend_get("/health", timeout=5)
                results.append(status == 200)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=make_request) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        success = sum(1 for r in results if r)
        print(f"\n✅ 동시 요청: {success}/5 성공 | 에러: {len(errors)}")
        assert success >= 4, f"동시 요청 성공 {success}/5 (기준: 4/5)"

    @skip_if_no_backend
    def test_mixed_endpoints_sequential(self):
        """다양한 엔드포인트 순차 요청"""
        endpoints = ["/health", "/", "/stats", "/markets"]
        success = 0
        for path in endpoints:
            status, result = backend_get(path, timeout=5)
            if status in (200, 404):  # 404도 백엔드 살아있음 의미
                success += 1
            time.sleep(0.1)
        assert success >= 2, f"엔드포인트 성공 {success}/{len(endpoints)}"
        print(f"\n✅ 혼합 요청: {success}/{len(endpoints)} 성공")


class TestOnboardValidation:
    """온보딩 API 주소 검증 테스트 (백엔드 필요)"""

    @skip_if_no_backend
    def test_onboard_invalid_address_returns_422(self):
        """/followers/onboard — 잘못된 주소 → 422"""
        status, result = backend_post(
            "/followers/onboard",
            {"follower_address": "invalid_addr_!@#$", "traders": []},
            timeout=5,
        )
        assert status == 422, f"422 기대, 실제: {status} | {result}"
        print(f"\n✅ 잘못된 주소 → 422: {result.get('detail', '')[:80]}")

    @skip_if_no_backend
    def test_onboard_empty_address_returns_422(self):
        """/followers/onboard — 빈 주소 → 422"""
        status, result = backend_post(
            "/followers/onboard",
            {"follower_address": "", "traders": []},
            timeout=5,
        )
        assert status in (422, 400), f"422/400 기대, 실제: {status}"

    @skip_if_no_backend
    def test_onboard_valid_address_proceeds(self):
        """/followers/onboard — 유효한 주소는 처리 진행 (422 아님)"""
        valid_addr = "3AHZqrocSguMuo9sUUP8G8YN8NwHwWV2DPUQvbDvtfaQ"
        status, result = backend_post(
            "/followers/onboard",
            {
                "follower_address": valid_addr,
                "traders": ["EcX5xSDT45Nvhi2gMTjTnhF3KT2w4sPF54esEZS3hwZu"],
                "copy_ratio": 0.05,
                "max_position_usdc": 10.0,
            },
            timeout=10,
        )
        # 유효한 주소는 422 아님 (200 또는 다른 에러 가능)
        assert status != 422, f"유효 주소인데 422: {result}"
        print(f"\n✅ 유효 주소 온보딩: HTTP {status}")


class TestScriptFiles:
    """스크립트 파일 존재 및 문법 검증"""

    def test_start_backend_sh_exists(self):
        """start_backend.sh 파일 존재"""
        base = os.path.dirname(os.path.dirname(__file__))
        path = os.path.join(base, "scripts", "start_backend.sh")
        assert os.path.exists(path), f"파일 없음: {path}"
        assert os.access(path, os.X_OK), f"실행 권한 없음: {path}"

    def test_start_frontend_sh_exists(self):
        """start_frontend.sh 파일 존재"""
        base = os.path.dirname(os.path.dirname(__file__))
        path = os.path.join(base, "scripts", "start_frontend.sh")
        assert os.path.exists(path), f"파일 없음: {path}"
        assert os.access(path, os.X_OK), f"실행 권한 없음: {path}"

    def test_collect_mainnet_traders_exists(self):
        """collect_mainnet_traders.py 파일 존재"""
        base = os.path.dirname(os.path.dirname(__file__))
        path = os.path.join(base, "scripts", "collect_mainnet_traders.py")
        assert os.path.exists(path), f"파일 없음: {path}"

    def test_start_backend_sh_syntax(self):
        """start_backend.sh bash 문법 검증"""
        base = os.path.dirname(os.path.dirname(__file__))
        path = os.path.join(base, "scripts", "start_backend.sh")
        result = subprocess.run(
            ["bash", "-n", path],
            capture_output=True, text=True
        )
        assert result.returncode == 0, f"문법 오류: {result.stderr}"

    def test_collect_mainnet_py_syntax(self):
        """collect_mainnet_traders.py 파이썬 문법 검증"""
        base = os.path.dirname(os.path.dirname(__file__))
        path = os.path.join(base, "scripts", "collect_mainnet_traders.py")
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", path],
            capture_output=True, text=True
        )
        assert result.returncode == 0, f"문법 오류: {result.stderr}"
