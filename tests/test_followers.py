"""
TC-FOLLOWERS: RISK_PRESETS 프리셋 검증 TC
2026-03-19 메인넷 실측 기반 확정값 검증
"""
import sys
import os

# 프로젝트 루트를 path에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.routers.followers import (
    RISK_PRESETS,
    DEFAULT_COPY_RATIO,
    DEFAULT_MAX_POS_USDC,
    TRADER_S,
    TRADER_A,
)


class TestRiskPresets:
    """RISK_PRESETS 4종 프리셋 구조 및 값 검증"""

    def test_all_presets_exist(self):
        """RISK_PRESETS에 4가지 키 존재"""
        assert set(RISK_PRESETS.keys()) == {"default", "conservative", "balanced", "aggressive"}

    def test_conservative_single_trader(self):
        """conservative: S등급 트레이더 1명만"""
        assert len(RISK_PRESETS["conservative"]["traders"]) == 1

    def test_aggressive_has_most_traders(self):
        """aggressive 트레이더 수 > balanced 트레이더 수"""
        assert len(RISK_PRESETS["aggressive"]["traders"]) > len(RISK_PRESETS["balanced"]["traders"])

    def test_default_copy_ratio_valid(self):
        """모든 프리셋의 copy_ratio가 0.01 ~ 1.0 범위"""
        for preset in RISK_PRESETS.values():
            assert 0.01 <= preset["copy_ratio"] <= 1.0, (
                f"copy_ratio 범위 오류: {preset['copy_ratio']}"
            )

    def test_expected_roi_ascending(self):
        """ROI 순서 보장: conservative < default < balanced < aggressive"""
        assert (
            RISK_PRESETS["conservative"]["expected_monthly_roi_pct"]
            < RISK_PRESETS["default"]["expected_monthly_roi_pct"]
            < RISK_PRESETS["balanced"]["expected_monthly_roi_pct"]
            < RISK_PRESETS["aggressive"]["expected_monthly_roi_pct"]
        ), (
            f"ROI 순서 오류: "
            f"conservative={RISK_PRESETS['conservative']['expected_monthly_roi_pct']}% "
            f"default={RISK_PRESETS['default']['expected_monthly_roi_pct']}% "
            f"balanced={RISK_PRESETS['balanced']['expected_monthly_roi_pct']}% "
            f"aggressive={RISK_PRESETS['aggressive']['expected_monthly_roi_pct']}%"
        )


class TestDefaultValues:
    """DEFAULT_ 상수값 검증"""

    def test_default_copy_ratio_is_10pct(self):
        """DEFAULT_COPY_RATIO = 0.10 (10%)"""
        assert DEFAULT_COPY_RATIO == 0.10

    def test_default_max_pos_is_300(self):
        """DEFAULT_MAX_POS_USDC = 300.0 (최적화 확정값)"""
        assert DEFAULT_MAX_POS_USDC == 300.0

    def test_default_traders_are_s_plus_a1(self):
        """default 프리셋: S 1명 + A ROI 1위 = 총 2명"""
        assert len(RISK_PRESETS["default"]["traders"]) == 2

    def test_aggressive_trader_count(self):
        """aggressive: S 1명 + A 8명 = 총 9명"""
        assert len(RISK_PRESETS["aggressive"]["traders"]) == 9


class TestTraderData:
    """메인넷 확정 트레이더 목록 검증"""

    def test_trader_s_count(self):
        """TRADER_S: S등급 1명"""
        assert len(TRADER_S) == 1

    def test_trader_a_count(self):
        """TRADER_A: A등급 8명"""
        assert len(TRADER_A) == 8

    def test_trader_s_address_format(self):
        """TRADER_S 주소가 Solana base58 형식 (32-44자)"""
        import re
        pat = re.compile(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$')
        for addr in TRADER_S:
            assert pat.match(addr), f"S등급 주소 형식 오류: {addr}"

    def test_trader_a_no_duplicates(self):
        """TRADER_A 주소에 중복 없음"""
        assert len(TRADER_A) == len(set(TRADER_A))
