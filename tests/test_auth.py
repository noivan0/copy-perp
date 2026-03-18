"""
tests/test_auth.py — Privy JWT 검증 + 인증/인가 + 입력값 검증 테스트

TC 목록:
  [AUTH-001] 정상 Solana 주소 → 통과
  [AUTH-002] 잘못된 주소 형식 → 422
  [AUTH-003] 빈 주소 → 422
  [AUTH-004] 특수문자 주소 → 422 or 404
  [AUTH-005] SQL Injection 주소 → 422
  [AUTH-006] XSS 페이로드 주소 → 422
  [AUTH-007] 거대 payload → 422 or 413
  [AUTH-008] copy_ratio 범위 위반 → 422
  [AUTH-009] max_position_usdc 범위 위반 → 422
  [AUTH-010] 변조 JWT (서명 불일치) → 401 or 폴백
  [AUTH-011] 가짜 JWT (완전히 임의) → 처리됨
  [AUTH-012] 에러 응답 형식 일관성 (스택트레이스 없음)
  [AUTH-013] 500 응답에 traceback 없음
  [AUTH-014] Rate Limit 아닌 정상 요청은 항상 200
  [AUTH-015] DELETE follower_address 없음 → 422
"""

import sys, os, json, base64, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import requests

BASE = "http://localhost:8001"
VALID_TRADER   = "4UBH19qUbXEaqyz9fKrFHuvj8BPMoM87H71s1YPKyGYq"
VALID_FOLLOWER = "3AHZqrocSguMuo9sUUP8G8YN8NwHwWV2DPUQvbDvtfaQ"


def backend_get(path, **kw):
    try:
        r = requests.get(BASE + path, timeout=8, **kw)
        return r.status_code, r.json()
    except ConnectionRefusedError:
        pytest.skip("백엔드 미기동")
    except Exception as e:
        return 0, {"error": str(e)}


def backend_post(path, body, **kw):
    try:
        r = requests.post(BASE + path, json=body, timeout=8, **kw)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text
    except ConnectionRefusedError:
        pytest.skip("백엔드 미기동")
    except Exception as e:
        return 0, {"error": str(e)}


def backend_delete(path, **kw):
    try:
        r = requests.delete(BASE + path, timeout=8, **kw)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text
    except ConnectionRefusedError:
        pytest.skip("백엔드 미기동")
    except Exception as e:
        return 0, {"error": str(e)}


# ──────────────────────────────────────────────────
# 1. 입력값 검증 (Input Validation)
# ──────────────────────────────────────────────────

class TestInputValidation:

    def test_auth_001_valid_address_passes(self):
        """[AUTH-001] 유효한 Solana 주소 → 200 or 409"""
        code, _ = backend_post("/follow", {
            "trader_address": VALID_TRADER,
            "follower_address": VALID_FOLLOWER,
            "copy_ratio": 0.05,
            "max_position_usdc": 50.0,
        })
        assert code in (200, 409), f"유효한 주소인데 거부: {code}"

    def test_auth_002_invalid_trader_address(self):
        """[AUTH-002] 잘못된 트레이더 주소 → 422"""
        code, _ = backend_post("/follow", {
            "trader_address": "not_a_valid_address",
            "follower_address": VALID_FOLLOWER,
            "copy_ratio": 0.05,
            "max_position_usdc": 50.0,
        })
        assert code in (400, 422), f"잘못된 주소가 통과됨: {code}"  # FastAPI: 422(Pydantic), 400(수동 raise)

    def test_auth_003_empty_address(self):
        """[AUTH-003] 빈 주소 → 422"""
        code, _ = backend_post("/follow", {
            "trader_address": "",
            "follower_address": VALID_FOLLOWER,
            "copy_ratio": 0.05,
            "max_position_usdc": 50.0,
        })
        assert code in (400, 422), f"빈 주소가 통과됨: {code}"  # FastAPI: 422(Pydantic), 400(수동 raise)

    def test_auth_004_special_chars_address(self):
        """[AUTH-004] 특수문자 주소 → 422 or 404 (500 아님)"""
        code, resp = backend_get("/traders/!!invalid!!")
        assert code != 500, f"특수문자 주소에서 500 발생: {resp}"
        assert code in (404, 400, 422), f"예상 외 코드: {code}"

    def test_auth_005_sql_injection_address(self):
        """[AUTH-005] SQL Injection 주소 → 422 차단"""
        sqli_payloads = [
            "'; DROP TABLE traders; --",
            "1' OR '1'='1",
            "1; SELECT * FROM followers;",
        ]
        for payload in sqli_payloads:
            code, _ = backend_post("/follow", {
                "trader_address": payload,
                "follower_address": VALID_FOLLOWER,
                "copy_ratio": 0.1,
                "max_position_usdc": 100,
            })
            assert code in (422, 400, 429), \
                f"SQL Injection 미차단: '{payload[:30]}' → {code}"

    def test_auth_006_xss_payload_not_reflected(self):
        """[AUTH-006] XSS 페이로드 → 응답에 미반영"""
        xss = "<script>alert(1)</script>"
        code, resp = backend_get(f"/traders?search={xss}")
        resp_str = json.dumps(resp) if isinstance(resp, dict) else str(resp)
        assert xss not in resp_str, f"XSS 페이로드가 응답에 반영됨: {resp_str[:100]}"

    def test_auth_007_huge_payload(self):
        """[AUTH-007] 거대 payload → 422 or 413 (서버 다운 없음)"""
        code, _ = backend_post("/follow", {
            "trader_address": "A" * 10_000,
            "follower_address": VALID_FOLLOWER,
            "copy_ratio": 0.1,
            "max_position_usdc": 100,
        })
        assert code in (422, 400, 413, 429), f"거대 payload 처리 오류: {code}"

    def test_auth_008_copy_ratio_out_of_range(self):
        """[AUTH-008] copy_ratio 범위 위반 → 422"""
        for bad_ratio in [0.0, 0.001, 1.1, -0.1, 999]:
            code, _ = backend_post("/follow", {
                "trader_address": VALID_TRADER,
                "follower_address": VALID_FOLLOWER,
                "copy_ratio": bad_ratio,
                "max_position_usdc": 50.0,
            })
            assert code == 422, \
                f"copy_ratio={bad_ratio} 가 통과됨: {code}"

    def test_auth_009_max_position_out_of_range(self):
        """[AUTH-009] max_position_usdc 범위 위반 → 422"""
        for bad_val in [0, -10, 0.5, 10_001, 999_999]:
            code, _ = backend_post("/follow", {
                "trader_address": VALID_TRADER,
                "follower_address": VALID_FOLLOWER,
                "copy_ratio": 0.1,
                "max_position_usdc": bad_val,
            })
            assert code == 422, \
                f"max_position_usdc={bad_val} 가 통과됨: {code}"

    def test_auth_015_delete_without_follower_address(self):
        """[AUTH-015] DELETE follower_address 없으면 → 422"""
        code, resp = backend_delete(f"/follow/{VALID_TRADER}")
        assert code == 422, f"follower_address 없는데 {code}: {resp}"


# ──────────────────────────────────────────────────
# 2. Privy JWT 검증
# ──────────────────────────────────────────────────

class TestPrivyJWT:
    """
    Privy JWT 검증 테스트.
    실제 Privy 발급 JWT 없이도 변조/가짜 케이스 검증 가능.
    """

    @staticmethod
    def _make_fake_jwt(payload_override=None):
        """검증 실패할 가짜 JWT 생성 (서명 불일치)"""
        header = base64.urlsafe_b64encode(
            json.dumps({"alg": "ES256", "typ": "JWT"}).encode()
        ).rstrip(b"=").decode()
        payload = {"sub": "did:privy:fake", "iss": "privy.io", "exp": 9999999999}
        if payload_override:
            payload.update(payload_override)
        payload_enc = base64.urlsafe_b64encode(
            json.dumps(payload).encode()
        ).rstrip(b"=").decode()
        return f"{header}.{payload_enc}.FAKESIGNATURE"

    def test_auth_010_tampered_jwt_handled(self):
        """[AUTH-010] 변조 JWT → 서버가 처리됨 (401 or 주소 기반 폴백)"""
        fake_jwt = self._make_fake_jwt()
        code, resp = backend_post("/followers/onboard", {
            "follower_address": VALID_FOLLOWER,
            "copy_ratio": 0.05,
            "max_position_usdc": 50.0,
            "traders": [VALID_TRADER],
            "privy_token": fake_jwt,
        })
        # 401 (JWT 거부) 또는 200 (주소 기반 폴백) 모두 허용
        # 중요한 것: 500이 아닐 것
        assert code in (200, 401, 403), \
            f"변조 JWT에서 예상 외 응답: {code} — {str(resp)[:100]}"

    def test_auth_011_completely_invalid_jwt(self):
        """[AUTH-011] 완전히 가짜 JWT → 서버 크래시 없음"""
        garbage_jwts = [
            "not.a.jwt",
            "eyJ.eyJ.sig",
            "!!!invalid!!!",
            "",
            "a" * 1000,
        ]
        for jwt_val in garbage_jwts:
            code, resp = backend_post("/followers/onboard", {
                "follower_address": VALID_FOLLOWER,
                "copy_ratio": 0.05,
                "max_position_usdc": 50.0,
                "traders": [VALID_TRADER],
                "privy_token": jwt_val,
            })
            assert code in (200, 401, 403, 422), \
                f"가짜 JWT '{jwt_val[:20]}...' → 서버 오류: {code}"
            # 500이면 크리티컬
            assert code != 500, f"JWT 처리 중 500 에러: {resp}"


# ──────────────────────────────────────────────────
# 3. 에러 응답 형식 일관성
# ──────────────────────────────────────────────────

class TestErrorResponseFormat:

    def test_auth_012_404_response_format(self):
        """[AUTH-012] 404 응답은 JSON {"error": ...} 형식"""
        try:
            r = requests.get(BASE + "/nonexistent_endpoint_xyz", timeout=5)
        except ConnectionRefusedError:
            pytest.skip("백엔드 미기동")
        assert r.status_code == 404
        try:
            d = r.json()
            assert "error" in d, f"404 응답에 'error' 키 없음: {d}"
        except json.JSONDecodeError:
            pytest.fail(f"404 응답이 JSON 아님: {r.text[:100]}")

    def test_auth_013_no_traceback_in_error(self):
        """[AUTH-013] 에러 응답에 스택트레이스 없음"""
        endpoints_to_probe = [
            "/nonexistent",
            f"/traders/{'A'*100}",
        ]
        forbidden = ["Traceback", "traceback", "File \"", "line ", "raise "]
        for ep in endpoints_to_probe:
            try:
                r = requests.get(BASE + ep, timeout=5)
            except ConnectionRefusedError:
                pytest.skip("백엔드 미기동")
            for word in forbidden:
                assert word not in r.text, \
                    f"응답에 스택트레이스 포함: GET {ep} → '{word}' 발견"

    def test_auth_014_valid_requests_always_200(self):
        """[AUTH-014] 정상 요청은 항상 200"""
        endpoints = [
            "/health", "/healthz", "/traders?limit=5",
            "/stats", "/markets", "/config",
        ]
        for ep in endpoints:
            code, _ = backend_get(ep)
            assert code == 200, f"{ep} → {code} (기대: 200)"
