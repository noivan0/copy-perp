"""
tests/test_verified_pnl.py — Verified PnL Engine 테스트

'이 트레이더를 복사했을 때 실제로 얼마 벌었는가'의 계산 정확성과
신뢰도 등급 체계 검증
"""
import pytest, sys, os, math
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.verified_pnl import (
    compute_verified_pnl, build_trust_report,
    COPY_REALISM_FACTOR, TOTAL_FEE_PCT,
    GRADE_THRESHOLDS, PnLProof,
)


# ── 픽스처 ────────────────────────────────────────────────────

def _make_trader(alias, grade, crs, roi_30d, pnl_30d=50000, equity=100000,
                 pnl_7d=0, pnl_1d=0, consistency=3, warns=None):
    """테스트용 CRS 트레이더 데이터"""
    return {
        "alias":   alias,
        "address": f"Test{alias}AAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "grade":   grade,
        "crs":     crs,
        "disqualified": False,
        "momentum_score":     80.0,
        "profitability_score": 85.0,
        "risk_score":          82.0,
        "warnings": warns or [],
        "raw": {
            "roi_30d":     roi_30d,
            "equity":      equity,
            "pnl_30d":     pnl_30d,
            "pnl_7d":      pnl_7d,
            "pnl_1d":      pnl_1d,
            "consistency": consistency,
        },
    }

S1 = _make_trader("AlphaS",  "S", 86.0, roi_30d=51.5, pnl_30d=126020, equity=244782)
S2 = _make_trader("BetaS",   "S", 82.0, roi_30d=43.6, pnl_30d=82248,  equity=188538)
A1 = _make_trader("GammaA",  "A", 78.0, roi_30d=35.9, pnl_30d=77527,  equity=215928)
A2 = _make_trader("DeltaA",  "A", 71.0, roi_30d=20.0, pnl_30d=30000,  equity=150000)
B1 = _make_trader("EpsilonB","B", 58.0, roi_30d=12.0, pnl_30d=15000,  equity=125000)
DQ = {**_make_trader("DQed","C", 40.0, roi_30d=5.0),  "disqualified": True}
TRADERS_ALL = [S1, S2, A1, A2, B1, DQ]


# ── compute_verified_pnl ─────────────────────────────────────

class TestComputeVerifiedPnl:

    def test_empty_input(self):
        proof = compute_verified_pnl([], capital=10000)
        assert proof.net_pnl == 0.0
        assert proof.confidence_level == "LOW"
        assert len(proof.traders) == 0

    def test_grade_filter_s_only(self):
        proof = compute_verified_pnl(TRADERS_ALL, min_grade="S")
        grades = [v.grade for v in proof.traders]
        assert all(g == "S" for g in grades), f"S 이하 포함됨: {grades}"
        assert len(proof.traders) == 2

    def test_grade_filter_a_plus(self):
        proof = compute_verified_pnl(TRADERS_ALL, min_grade="A")
        grades = [v.grade for v in proof.traders]
        assert all(g in ("S","A") for g in grades)
        assert len(proof.traders) == 4

    def test_disqualified_excluded(self):
        traders_with_dq = [S1, DQ]
        proof = compute_verified_pnl(traders_with_dq, min_grade="C")
        addrs = [v.address for v in proof.traders]
        assert not any("DQed" in a for a in addrs), "DQ 트레이더가 포함됨"

    def test_copy_pnl_formula(self):
        """팔로워 수익 = 할당자본 × ROI × 현실화계수"""
        capital = 10_000.0
        copy_ratio = 0.10
        proof = compute_verified_pnl([S1], capital=capital, copy_ratio=copy_ratio)
        v = proof.traders[0]

        allocated = capital * copy_ratio
        expected_gross = allocated * (S1["raw"]["roi_30d"] / 100) * COPY_REALISM_FACTOR
        assert v.sim_pnl_30d == pytest.approx(expected_gross, rel=0.01)

    def test_fee_deduction(self):
        """수수료 = allocated × fee_pct × est_trades"""
        capital = 10_000.0
        copy_ratio = 0.10
        period_days = 30
        proof = compute_verified_pnl([S1], capital=capital, copy_ratio=copy_ratio,
                                      period_days=period_days)
        v = proof.traders[0]

        allocated = capital * copy_ratio
        est_trades = period_days * 2
        expected_fee = allocated * TOTAL_FEE_PCT * est_trades
        assert v.sim_fee_30d == pytest.approx(expected_fee, rel=0.01)

    def test_net_pnl_is_gross_minus_fee(self):
        proof = compute_verified_pnl([S1, S2], capital=10000, copy_ratio=0.10)
        expected_net = proof.total_sim_pnl - proof.total_sim_fee
        assert proof.net_pnl == pytest.approx(expected_net, abs=0.01)

    def test_roi_vs_capital(self):
        """net_roi_pct = net_pnl / capital × 100"""
        capital = 5000.0
        proof = compute_verified_pnl([A1], capital=capital, copy_ratio=0.15)
        expected_roi = proof.net_pnl / capital * 100
        assert proof.net_roi_pct == pytest.approx(expected_roi, abs=0.01)

    def test_period_scaling(self):
        """7일 시뮬은 30일 시뮬의 7/30"""
        proof_30 = compute_verified_pnl([S1], capital=10000, period_days=30)
        proof_7  = compute_verified_pnl([S1], capital=10000, period_days=7)
        ratio = proof_7.net_pnl / proof_30.net_pnl
        assert ratio == pytest.approx(7/30, rel=0.05)

    def test_multiple_traders_sum(self):
        """여러 트레이더 수익 합산"""
        proof_a = compute_verified_pnl([S1], capital=10000, copy_ratio=0.10)
        proof_b = compute_verified_pnl([S2], capital=10000, copy_ratio=0.10)
        proof_ab = compute_verified_pnl([S1, S2], capital=10000, copy_ratio=0.10)

        expected_gross = proof_a.total_sim_pnl + proof_b.total_sim_pnl
        assert proof_ab.total_sim_pnl == pytest.approx(expected_gross, rel=0.01)

    def test_confidence_high(self):
        """S등급 3명 이상 → HIGH 신뢰도"""
        s_traders = [
            _make_trader(f"S{i}", "S", 82.0 + i, roi_30d=45.0)
            for i in range(3)
        ]
        proof = compute_verified_pnl(s_traders, min_grade="S")
        assert proof.confidence_level == "HIGH"

    def test_confidence_medium(self):
        """A등급 2명 → MEDIUM"""
        proof = compute_verified_pnl([A1, A2], min_grade="A")
        assert proof.confidence_level in ("MEDIUM", "LOW")

    def test_sharpe_positive(self):
        """수익 트레이더들 → Sharpe > 0"""
        proof = compute_verified_pnl([S1, S2, A1], min_grade="A")
        assert proof.portfolio_sharpe >= 0

    def test_survival_rate_range(self):
        proof = compute_verified_pnl([S1, S2], min_grade="S")
        assert 0 <= proof.survival_rate <= 100

    def test_grade_distribution(self):
        proof = compute_verified_pnl(TRADERS_ALL, min_grade="B")
        assert "S" in proof.grade_distribution
        assert "A" in proof.grade_distribution
        assert "B" in proof.grade_distribution
        assert proof.grade_distribution["S"] == 2
        assert proof.grade_distribution["A"] == 2
        assert proof.grade_distribution["B"] == 1

    def test_avg_crs_correct(self):
        proof = compute_verified_pnl([S1, A1], min_grade="A")
        expected = (S1["crs"] + A1["crs"]) / 2
        assert proof.avg_crs == pytest.approx(expected, abs=0.1)


# ── build_trust_report ─────────────────────────────────────

class TestBuildTrustReport:

    @pytest.fixture
    def proof(self):
        return compute_verified_pnl([S1, S2, A1], capital=10000,
                                     copy_ratio=0.10, min_grade="A")

    @pytest.fixture
    def report(self, proof):
        return build_trust_report(proof)

    def test_report_keys(self, report):
        for key in ("title", "confidence", "portfolio_summary", "per_trader",
                    "trust_basis", "benchmark", "disclaimers", "proof_note"):
            assert key in report, f"누락 키: {key}"

    def test_portfolio_summary_keys(self, report):
        ps = report["portfolio_summary"]
        for key in ("capital_usdc", "net_pnl_usdc", "net_roi_pct", "final_equity_usdc",
                    "sharpe_ratio", "traders_count", "avg_crs"):
            assert key in ps, f"summary 누락: {key}"

    def test_final_equity_equals_capital_plus_pnl(self, report):
        ps = report["portfolio_summary"]
        assert ps["final_equity_usdc"] == pytest.approx(
            ps["capital_usdc"] + ps["net_pnl_usdc"], abs=0.01
        )

    def test_per_trader_count(self, report):
        assert len(report["per_trader"]) == 3

    def test_per_trader_fields(self, report):
        for t in report["per_trader"]:
            for k in ("alias", "grade", "crs", "trader_roi_30d_pct",
                      "follower_net_pnl", "follower_roi_pct", "data_source"):
                assert k in t, f"per_trader 누락: {k}"

    def test_per_trader_data_source(self, report):
        for t in report["per_trader"]:
            assert "Hyperliquid" in t["data_source"]

    def test_benchmark_has_copyperp(self, report):
        assert "copyperp_verified" in report["benchmark"]
        cp = report["benchmark"]["copyperp_verified"]
        assert "roi_pct" in cp
        assert "pnl_usdc" in cp
        assert "sharpe" in cp

    def test_benchmark_copyperp_roi_matches_proof(self, proof, report):
        cp = report["benchmark"]["copyperp_verified"]
        assert cp["roi_pct"] == pytest.approx(proof.net_roi_pct, abs=0.01)

    def test_trust_basis_structure(self, report):
        tb = report["trust_basis"]
        assert "data_source" in tb
        assert "crs_components" in tb
        assert "realism_adjustments" in tb
        assert "grade_criteria" in tb

    def test_grade_criteria_all_grades(self, report):
        gc = report["trust_basis"]["grade_criteria"]
        for g in ("S", "A", "B", "C"):
            assert g in gc, f"등급 기준 누락: {g}"

    def test_disclaimers_not_empty(self, report):
        assert len(report["disclaimers"]) >= 3

    def test_confidence_icon(self, report):
        assert report["confidence_icon"] in ("🔒", "✅", "⚠️")

    def test_grade_label_s(self, report):
        s_traders = [t for t in report["per_trader"] if t["grade"] == "S"]
        for t in s_traders:
            assert "Elite" in t["grade_label"]

    def test_grade_label_a(self, report):
        a_traders = [t for t in report["per_trader"] if t["grade"] == "A"]
        for t in a_traders:
            assert "Top" in t["grade_label"]


# ── 등급 임계값 일관성 ──────────────────────────────────────

class TestGradeThresholds:

    def test_s_grade_strictest(self):
        """S등급이 A보다 엄격해야 함"""
        s = GRADE_THRESHOLDS["S"]
        a = GRADE_THRESHOLDS["A"]
        assert s[0] > a[0], "S CRS 최소값이 A보다 높아야 함"
        assert s[1] > a[1], "S ROI 최소값이 A보다 높아야 함"
        assert s[3] < a[3], "S MaxDD 최대값이 A보다 낮아야 함"

    def test_grade_order_crs(self):
        """등급 내림차순으로 CRS 최소값도 내림차순"""
        grades = ["S", "A", "B", "C"]
        crs_mins = [GRADE_THRESHOLDS[g][0] for g in grades]
        assert crs_mins == sorted(crs_mins, reverse=True)

    def test_all_grades_defined(self):
        for g in ("S", "A", "B", "C"):
            assert g in GRADE_THRESHOLDS
            assert len(GRADE_THRESHOLDS[g]) == 5


# ── 실데이터 근사 검증 (integration) ─────────────────────────

class TestRealDataApproximation:
    """
    crs_result.json의 실제 S등급 트레이더 기준 검증
    7gV81bz9: CRS=86.1, ROI=51.48%, equity=244782
    """

    def test_real_trader_s_grade_pnl(self):
        real_s = _make_trader("7gV81bz9", "S", 86.1,
                               roi_30d=51.48, pnl_30d=126020, equity=244782,
                               consistency=4)
        capital = 10_000.0
        ratio   = 0.10
        proof = compute_verified_pnl([real_s], capital=capital, copy_ratio=ratio)
        v = proof.traders[0]

        # 할당 $1,000 × 51.48% × 82% ≈ $422
        expected_gross = 1000 * 0.5148 * COPY_REALISM_FACTOR
        assert v.sim_pnl_30d == pytest.approx(expected_gross, rel=0.02)

    def test_real_warning_trader_still_included(self):
        """경고 있어도 DQ 아니면 포함"""
        warned = _make_trader("EcX5", "A", 70.4, roi_30d=82.5,
                               warns=["단발성 의심 (7일에 100% 집중)"])
        proof = compute_verified_pnl([warned], min_grade="A")
        assert len(proof.traders) == 1
        assert proof.traders[0].warnings != []

    def test_capital_1k_s_grade(self):
        """$1,000 소자본 · S등급 → 양수 수익"""
        s_traders = [S1, S2]
        proof = compute_verified_pnl(s_traders, capital=1000, copy_ratio=0.12,
                                      min_grade="S")
        assert proof.net_pnl > 0, "S등급 복사 시 순이익이 양수여야 함"

    def test_fee_not_exceed_gross(self):
        """정상 케이스에서 수수료 < 총수익"""
        proof = compute_verified_pnl([S1, A1], capital=10000, copy_ratio=0.10)
        assert proof.total_sim_fee < proof.total_sim_pnl
