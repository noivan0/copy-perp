"""
TC-FOLLOWERS: RISK_PRESETS 프리셋 검증 TC
2026-04-13 업데이트: TRADER_S=[] (S등급 공석), TRADER_A 8명 기준
"""
import sys
import os
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.routers.followers import (
    RISK_PRESETS,
    DEFAULT_COPY_RATIO,
    DEFAULT_MAX_POS_USDC,
    TRADER_S,
    TRADER_A,
)

_SOLANA_PAT = re.compile(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$')


class TestRiskPresets:
    """RISK_PRESETS 4종 프리셋 구조 및 값 검증"""

    def test_all_presets_exist(self):
        """RISK_PRESETS에 4가지 키 존재"""
        assert set(RISK_PRESETS.keys()) == {"default", "conservative", "balanced", "aggressive"}

    def test_all_presets_have_required_keys(self):
        """모든 프리셋에 필수 키 존재"""
        required = {"traders", "copy_ratio", "max_position_usdc", "expected_monthly_roi_pct"}
        for name, preset in RISK_PRESETS.items():
            assert required <= set(preset.keys()), f"{name} 프리셋 키 누락"

    def test_conservative_single_trader(self):
        """conservative: 최상위 1명만"""
        assert len(RISK_PRESETS["conservative"]["traders"]) == 1

    def test_default_single_trader(self):
        """default: 최소 1명 이상"""
        assert len(RISK_PRESETS["default"]["traders"]) >= 1

    def test_aggressive_has_most_traders(self):
        """aggressive 트레이더 수 >= balanced"""
        assert len(RISK_PRESETS["aggressive"]["traders"]) >= len(RISK_PRESETS["balanced"]["traders"])

    def test_balanced_more_than_default(self):
        """balanced 트레이더 수 > default"""
        assert len(RISK_PRESETS["balanced"]["traders"]) > len(RISK_PRESETS["default"]["traders"])

    def test_default_copy_ratio_valid(self):
        """모든 프리셋 copy_ratio가 0.01~1.0 범위"""
        for preset in RISK_PRESETS.values():
            assert 0.01 <= preset["copy_ratio"] <= 1.0

    def test_expected_roi_ascending(self):
        """ROI 순서: conservative < default < balanced < aggressive"""
        c = RISK_PRESETS["conservative"]["expected_monthly_roi_pct"]
        d = RISK_PRESETS["default"]["expected_monthly_roi_pct"]
        b = RISK_PRESETS["balanced"]["expected_monthly_roi_pct"]
        a = RISK_PRESETS["aggressive"]["expected_monthly_roi_pct"]
        assert c < d < b < a, f"ROI 순서 오류: {c} {d} {b} {a}"

    def test_all_trader_addresses_valid(self):
        """모든 프리셋 트레이더 주소 Solana 형식"""
        for name, preset in RISK_PRESETS.items():
            for addr in preset["traders"]:
                assert _SOLANA_PAT.match(addr), f"{name}: 주소 형식 오류 {addr}"

    def test_no_duplicate_traders_per_preset(self):
        """각 프리셋 내 중복 트레이더 없음"""
        for name, preset in RISK_PRESETS.items():
            traders = preset["traders"]
            assert len(traders) == len(set(traders)), f"{name}: 트레이더 중복"


class TestDefaultValues:
    """DEFAULT_ 상수값 검증"""

    def test_default_copy_ratio_is_10pct(self):
        """DEFAULT_COPY_RATIO = 0.10"""
        assert DEFAULT_COPY_RATIO == 0.10

    def test_default_max_pos_is_300(self):
        """DEFAULT_MAX_POS_USDC = 300.0"""
        assert DEFAULT_MAX_POS_USDC == 300.0

    def test_aggressive_has_all_traders(self):
        """aggressive: 전체 트레이더 포함"""
        all_traders = (TRADER_S + TRADER_A) if TRADER_S else TRADER_A
        assert len(RISK_PRESETS["aggressive"]["traders"]) == len(all_traders)


class TestTraderData:
    """메인넷 확정 트레이더 목록 검증"""

    def test_trader_s_is_list(self):
        """TRADER_S는 리스트 (공석 허용)"""
        assert isinstance(TRADER_S, list)

    def test_trader_a_count(self):
        """TRADER_A: A등급 8명"""
        assert len(TRADER_A) == 8

    def test_trader_a_no_duplicates(self):
        """TRADER_A 중복 없음"""
        assert len(TRADER_A) == len(set(TRADER_A))

    def test_trader_a_address_format(self):
        """TRADER_A 주소 Solana base58 형식"""
        for addr in TRADER_A:
            assert _SOLANA_PAT.match(addr), f"A등급 주소 오류: {addr}"

    def test_trader_s_address_format(self):
        """TRADER_S 주소 형식 (공석이면 스킵)"""
        for addr in TRADER_S:
            assert _SOLANA_PAT.match(addr), f"S등급 주소 오류: {addr}"
