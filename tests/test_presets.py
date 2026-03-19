"""
tests/test_presets.py — 시나리오 프리셋 시스템 테스트

TC-P01: get_preset("default")     → copy_ratio=0.08
TC-P02: get_preset("conservative")→ copy_ratio=0.10
TC-P03: get_preset("balanced")    → copy_ratio=0.12
TC-P04: get_preset("aggressive")  → copy_ratio=0.15
TC-P05: get_preset("invalid")     → default 반환 (KeyError 없음)
TC-P06: resolve_traders DB 없을 때 → FALLBACK_TRADERS 사용
TC-P07: get_preset_sim_pnl DB 없을 때 → estimated 반환, pnl_30d > 0
TC-P08: list_presets_with_sim     → 4개 반환
TC-P09: 모든 프리셋 expected_roi_30d_pct > 0
TC-P10: resolve_traders 반환 목록 길이 = n_traders
"""
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core.strategy_presets import (
    PRESETS,
    FALLBACK_TRADERS,
    get_preset,
    resolve_traders,
    get_preset_sim_pnl,
    list_presets_with_sim,
)

_NONEXIST_DB = "/tmp/nonexist_mainnet_tracker_test.db"


# ── TC-P01 ~ P04: 프리셋 파라미터 검증 ───────────────────────────────

def test_get_preset_default():
    """TC-P01: 기본형 copy_ratio=0.08"""
    p = get_preset("default")
    assert p["copy_ratio"] == 0.08, f"expected 0.08, got {p['copy_ratio']}"
    assert p["max_position_usdc"] == 40.0
    assert p["risk_level"] == 1
    assert p["n_traders"] == 2


def test_get_preset_conservative():
    """TC-P02: 안정형 copy_ratio=0.10"""
    p = get_preset("conservative")
    assert p["copy_ratio"] == 0.10
    assert p["max_position_usdc"] == 100.0
    assert p["risk_level"] == 2
    assert p["n_traders"] == 3


def test_get_preset_balanced():
    """TC-P03: 균형형 copy_ratio=0.12"""
    p = get_preset("balanced")
    assert p["copy_ratio"] == 0.12
    assert p["max_position_usdc"] == 150.0
    assert p["risk_level"] == 3
    assert p["n_traders"] == 4
    assert "A" in p["grade_filter"]


def test_get_preset_aggressive():
    """TC-P04: 공격형 copy_ratio=0.15"""
    p = get_preset("aggressive")
    assert p["copy_ratio"] == 0.15
    assert p["max_position_usdc"] == 200.0
    assert p["risk_level"] == 4
    assert p["sort_by"] == "roi_7d"


# ── TC-P05: 잘못된 키 → default 반환 ─────────────────────────────────

def test_get_preset_invalid_key():
    """TC-P05: 잘못된 키 → default fallback (KeyError 없음)"""
    p = get_preset("nonexistent_preset_xyz")
    assert p["key"] == "default"
    assert p["copy_ratio"] == 0.08


# ── TC-P06: resolve_traders — DB 없을 때 FALLBACK 사용 ───────────────

def test_resolve_traders_fallback_when_no_db():
    """TC-P06: DB 없을 때 FALLBACK_TRADERS 사용"""
    addrs = resolve_traders("default", db_path=_NONEXIST_DB)
    assert len(addrs) > 0, "FALLBACK_TRADERS에서 최소 1개 이상 반환해야 함"
    # FALLBACK_TRADERS의 S등급 주소에서 가져와야 함
    fb_s = FALLBACK_TRADERS.get("S", [])
    assert any(a in fb_s for a in addrs), f"FALLBACK S등급에서 가져와야 함, got: {addrs}"


def test_resolve_traders_fallback_conservative():
    """TC-P06b: conservative → S등급 3명 fallback"""
    addrs = resolve_traders("conservative", db_path=_NONEXIST_DB)
    assert len(addrs) == 3
    fb_s = FALLBACK_TRADERS.get("S", [])
    assert all(a in fb_s for a in addrs), f"conservative은 S등급만: {addrs}"


def test_resolve_traders_fallback_aggressive():
    """TC-P06c: aggressive → S+A등급 fallback"""
    addrs = resolve_traders("aggressive", db_path=_NONEXIST_DB)
    assert len(addrs) == 3
    fb_all = FALLBACK_TRADERS.get("S", []) + FALLBACK_TRADERS.get("A", [])
    assert all(a in fb_all for a in addrs), f"aggressive은 S+A등급: {addrs}"


# ── TC-P07: get_preset_sim_pnl — DB 없을 때 estimated 반환 ───────────

def test_preset_sim_pnl_no_db():
    """TC-P07: DB 없을 때 estimated 반환, pnl_30d > 0"""
    sim = get_preset_sim_pnl("conservative", capital=1000.0, db_path=_NONEXIST_DB)
    assert sim["data_source"] == "estimated"
    assert sim["pnl_30d"] > 0, f"pnl_30d should be positive, got {sim['pnl_30d']}"
    assert sim["roi_30d_pct"] > 0
    assert "traders" in sim


def test_preset_sim_pnl_all_presets_no_db():
    """TC-P07b: 4개 프리셋 모두 estimated 반환"""
    for key in ["default", "conservative", "balanced", "aggressive"]:
        sim = get_preset_sim_pnl(key, capital=1000.0, db_path=_NONEXIST_DB)
        assert sim["pnl_30d"] >= 0, f"{key}: pnl_30d should be >= 0"
        assert "roi_30d_pct" in sim


# ── TC-P08: list_presets_with_sim — 4개 반환 ─────────────────────────

def test_list_presets_with_sim_count():
    """TC-P08: 4개 프리셋 반환"""
    result = list_presets_with_sim(capital=1000.0, db_path=_NONEXIST_DB)
    assert len(result) == 4, f"4개 프리셋 필요, got {len(result)}"
    keys = [p["key"] for p in result]
    assert "default" in keys
    assert "conservative" in keys
    assert "balanced" in keys
    assert "aggressive" in keys


def test_list_presets_sim_pnl_structure():
    """TC-P08b: 각 프리셋에 sim_pnl 구조 포함"""
    result = list_presets_with_sim(capital=1000.0, db_path=_NONEXIST_DB)
    for p in result:
        assert "sim_pnl" in p, f"{p['key']} sim_pnl 없음"
        sim = p["sim_pnl"]
        assert "pnl_30d" in sim
        assert "roi_30d_pct" in sim
        assert sim["capital"] == 1000.0


# ── TC-P09: expected_roi_30d_pct > 0 ────────────────────────────────

def test_all_presets_expected_roi_positive():
    """TC-P09: 모든 프리셋의 expected_roi_30d_pct > 0"""
    for key, preset in PRESETS.items():
        roi = preset.get("expected_roi_30d_pct", 0)
        assert roi > 0, f"{key}: expected_roi_30d_pct={roi} should be > 0"


# ── TC-P10: resolve_traders 반환 길이 = n_traders ───────────────────

def test_resolve_traders_length():
    """TC-P10: 반환 목록 길이 = preset n_traders"""
    for key, preset in PRESETS.items():
        n = preset["n_traders"]
        addrs = resolve_traders(key, db_path=_NONEXIST_DB)
        assert len(addrs) == n, f"{key}: n_traders={n}, got {len(addrs)} → {addrs}"
        # 중복 없음
        assert len(set(addrs)) == len(addrs), f"{key}: 중복 주소 발견"


# ── TC-P11: PRESETS 구조 완결성 ─────────────────────────────────────

def test_presets_schema():
    """TC-P11: 모든 프리셋에 필수 필드 존재"""
    required = ["key", "label", "emoji", "copy_ratio", "max_position_usdc",
                "n_traders", "grade_filter", "sort_by", "risk_level",
                "expected_roi_30d_pct"]
    for key, preset in PRESETS.items():
        for field in required:
            assert field in preset, f"{key}: '{field}' 필드 없음"
        # grade_filter는 리스트
        assert isinstance(preset["grade_filter"], list)
        # risk_level 1~4
        assert 1 <= preset["risk_level"] <= 4, f"{key}: risk_level={preset['risk_level']}"
        # sort_by 유효값
        assert preset["sort_by"] in ("roi_30d", "roi_7d", "score", "stability"), \
            f"{key}: sort_by={preset['sort_by']}"
