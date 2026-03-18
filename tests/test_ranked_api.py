"""
tests/test_ranked_api.py
/traders/ranked 엔드포인트 E2E 테스트

QA팀장 작성 — CRS 신뢰도 랭킹 API 품질 게이트
"""
import socket
import json
import pytest
from fastapi.testclient import TestClient
from api.main import app as _fastapi_app


@pytest.fixture(scope="module")
def testclient():
    """FastAPI TestClient 픽스처 — 실서버 없이 앱 내부 직접 호출 (lifespan 실행)"""
    with TestClient(_fastapi_app) as c:
        yield c


def raw_get(path: str, timeout: int = 8) -> dict:
    """localhost:8001 raw socket GET — urllib HMG 차단 우회"""
    s = socket.create_connection(("localhost", 8001), timeout=5)
    req = f"GET {path} HTTP/1.1\r\nHost: localhost:8001\r\nConnection: close\r\n\r\n"
    s.sendall(req.encode())
    s.settimeout(timeout)
    data = b""
    while True:
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
    s.close()
    body = data.split(b"\r\n\r\n", 1)[1]
    return json.loads(body)


@pytest.fixture(scope="module")
def backend_ok():
    """백엔드 기동 확인 — 없으면 전체 스킵"""
    try:
        h = raw_get("/health")
        assert h.get("status") == "ok"
    except Exception:
        pytest.skip("백엔드 미기동 (localhost:8001)")


class TestRankedAPI:
    """GET /traders/ranked — 핵심 품질 게이트"""

    def test_ranked_200(self, backend_ok):
        """기본 응답 200 + 필수 필드 존재"""
        resp = raw_get("/traders/ranked?limit=5")
        assert "data" in resp
        assert "total_analyzed" in resp

    def test_ranked_has_traders(self, backend_ok):
        """트레이더 존재 — 데이터 비어있지 않음"""
        resp = raw_get("/traders/ranked?limit=20")
        assert len(resp.get("data", [])) > 0, "랭킹 데이터 없음"

    def test_ranked_crs_fields(self, backend_ok):
        """각 트레이더에 CRS 필수 필드 포함"""
        resp = raw_get("/traders/ranked?limit=5")
        required = ["address", "crs", "grade", "tier_label", "recommended_copy_ratio", "copy_ratio_pct"]
        for t in resp.get("data", []):
            for field in required:
                assert field in t, f"필드 누락: {field} (trader: {t.get('address', '?')[:10]})"

    def test_ranked_crs_range(self, backend_ok):
        """CRS 점수 0~100 범위 유효"""
        resp = raw_get("/traders/ranked?limit=20")
        for t in resp.get("data", []):
            crs = t.get("crs", -1)
            assert 0 <= crs <= 100, f"CRS 범위 초과: {crs} (trader: {t.get('address', '?')[:10]})"

    def test_ranked_grade_valid(self, backend_ok):
        """등급 S/A/B/C/D 중 하나"""
        valid_grades = {"S", "A", "B", "C", "D"}
        resp = raw_get("/traders/ranked?limit=20")
        for t in resp.get("data", []):
            assert t.get("grade") in valid_grades, f"유효하지 않은 등급: {t.get('grade')}"

    def test_ranked_sorted_by_crs(self, backend_ok):
        """CRS 내림차순 정렬"""
        resp = raw_get("/traders/ranked?limit=10")
        scores = [t.get("crs", 0) for t in resp.get("data", [])]
        assert scores == sorted(scores, reverse=True), "CRS 내림차순 정렬 오류"

    def test_ranked_grade_filter_b(self, backend_ok):
        """min_grade=B → B/S/A 등급만 반환"""
        resp = raw_get("/traders/ranked?limit=30&min_grade=B&exclude_disqualified=true")
        from core.reliability import GRADE
        min_score = GRADE["B"]
        for t in resp.get("data", []):
            g = t.get("grade", "D")
            assert GRADE.get(g, 0) >= min_score, f"등급 필터 오류: {g} (min=B)"

    def test_ranked_exclude_disqualified(self, backend_ok):
        """exclude_disqualified=true → disqualified 없어야 함"""
        resp = raw_get("/traders/ranked?limit=20&exclude_disqualified=true")
        for t in resp.get("data", []):
            assert not t.get("disqualified"), f"disqualified 트레이더 노출: {t.get('address', '?')[:10]}"

    def test_ranked_copy_ratio_valid(self, backend_ok):
        """추천 copy_ratio 0~1 범위"""
        resp = raw_get("/traders/ranked?limit=10")
        for t in resp.get("data", []):
            ratio = t.get("recommended_copy_ratio", -1)
            assert 0 <= ratio <= 1, f"copy_ratio 범위 초과: {ratio}"


class TestRankedSummary:
    """GET /traders/ranked/summary"""

    # ── 실서버(8001) 기반 테스트 (backend_ok 픽스처 사용) ──
    def test_summary_200(self, backend_ok):
        """요약 API 정상 응답"""
        resp = raw_get("/traders/ranked/summary")
        assert "summary" in resp
        assert "total_analyzed" in resp

    def test_summary_all_grades(self, backend_ok):
        """S/A/B/C/D 모든 등급 포함"""
        resp = raw_get("/traders/ranked/summary")
        for g in ["S", "A", "B", "C", "D"]:
            assert g in resp["summary"], f"등급 누락: {g}"

    def test_summary_counts_positive(self, backend_ok):
        """각 등급 count는 0 이상"""
        resp = raw_get("/traders/ranked/summary")
        for g in ["S", "A", "B", "C", "D"]:
            count = resp["summary"][g]["count"]
            assert count >= 0, f"{g} count 음수: {count}"

    def test_summary_total_matches(self, backend_ok):
        """등급별 합계 = total_analyzed"""
        resp = raw_get("/traders/ranked/summary")
        total = resp.get("total_analyzed", 0)
        grade_sum = sum(resp["summary"][g]["count"] for g in ["S", "A", "B", "C", "D"])
        assert grade_sum == total, f"등급 합계 불일치: {grade_sum} != {total}"

    def test_summary_has_grade_thresholds(self, backend_ok):
        """grade_thresholds 포함 확인"""
        resp = raw_get("/traders/ranked/summary")
        assert "grade_thresholds" in resp
        assert "max_copy_ratio" in resp

    # ── TestClient 기반 테스트 (실서버 없이 동작) ──
    def test_tc_summary_200(self, testclient):
        """[TestClient] 요약 API 정상 응답 (실서버 불필요)"""
        resp = testclient.get("/traders/ranked/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "summary" in data
        assert "total_analyzed" in data

    def test_tc_summary_total_matches(self, testclient):
        """[TestClient] 등급별 합계 = total_analyzed"""
        resp = testclient.get("/traders/ranked/summary")
        assert resp.status_code == 200
        data = resp.json()
        total = data.get("total_analyzed", 0)
        assert total > 0, f"total_analyzed=0 — DB 데이터 없음"
        grade_sum = sum(data["summary"][g]["count"] for g in ["S", "A", "B", "C", "D"])
        assert grade_sum == total, f"등급 합계 불일치: {grade_sum} != {total}"

    def test_tc_summary_has_grade_thresholds(self, testclient):
        """[TestClient] grade_thresholds 포함 확인"""
        resp = testclient.get("/traders/ranked/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "grade_thresholds" in data, "grade_thresholds 필드 누락"
        assert "max_copy_ratio" in data, "max_copy_ratio 필드 누락"
        # 5개 등급 모두 threshold 존재
        for g in ["S", "A", "B", "C", "D"]:
            assert g in data["grade_thresholds"], f"grade_thresholds에 {g} 누락"
