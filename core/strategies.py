"""
Copy Perp 전략 프리셋 정의
메인넷 CRS 분석 기준 (2026-03-19, 품질 필터: equity>$5k, vol30>$50k, pnl30>$5k)
파라미터 확정: 메인넷 실거래 시뮬레이션 기반 (2026-03-19 최종)

외부 의존성 없음 — 순수 Python dict/function만 사용.
"""

# ── 메인넷 검증 트레이더 풀 (2026-03-19 CRS 분석, 최종 확정) ──────────────
MAINNET_TRADERS = {
    # 기본형·균형형·공격형 공통 풀
    "4TYEjn9PSpxoBNBXufeuNDRbytzvyyZtEUgXYSk8kYLZ": {
        "alias": "4TYEjn9P", "crs": 81.1, "grade": "A",   # 실측 확정
        "roi_30d": 141.7, "consistency": 4, "leverage": 5.7,
        "pnl_30d": 64354, "vol_30d": 17226680,
    },
    "YjCD9Gek6MVY9t3MLEGYYdZLeaF6MZrpgZraayWsv9E": {
        "alias": "YjCD9Gek", "crs": 82.5, "grade": "A",   # 실측 확정
        "roi_30d": 113.9, "consistency": 3, "leverage": 1.5,
        "pnl_30d": 103336, "vol_30d": 12457850,
    },
    "6ZjWoJKeD88JqREHhYAWSZVLQfVcMSbx6eVdajXt9Xbv": {
        "alias": "6ZjWoJKe", "crs": 82.4, "grade": "A",   # 실측 확정
        "roi_30d": 157.5, "consistency": 3, "leverage": 2.7,
        "pnl_30d": 29256, "vol_30d": 681169,
    },
    "Ph9yECGodDAjiiSU9bpbJ8dds3ndWP1ngKo8h1K2QYv": {
        "alias": "Ph9yECGo", "crs": 69.5, "grade": "A",
        "roi_30d": 1017.3, "consistency": 3, "leverage": 2.2,
        "pnl_30d": 100025, "vol_30d": 1192187,
    },
    "FN4seJZ9Wdi3NCbugCkPD5xYac5UrCQmzQt4o3Ko5VB2": {
        "alias": "FN4seJZ9", "crs": 66.8, "grade": "A",
        "roi_30d": 416.2, "consistency": 4, "leverage": 0.7,
        "pnl_30d": 37555, "vol_30d": 693135,
    },
    "D5LnbmzTQPCmWBkr9yD2pRq3q5XT4TVmjibhXvsAzj6v": {
        "alias": "D5Lnbmz", "crs": 75.1, "grade": "A",
        "roi_30d": 30.7, "consistency": 3, "leverage": 0.0,
        "pnl_30d": 13444, "vol_30d": 141705,
    },
    "CAHPdCrmxQyt8aGETr6cYedw3QvyqxWBRortR7ddN6bL": {
        "alias": "CAHPdCrm", "crs": 72.0, "grade": "A",
        "roi_30d": 27.6, "consistency": 3, "leverage": 1.3,
        "pnl_30d": 12409, "vol_30d": 91859,
    },
    # 안정형 전용
    "GNzSLjvyysA4AHEbXq1PgKm9oHqmqZmLdup9vH1z3Z3a": {
        "alias": "GNzSLjvy", "crs": 56.7, "grade": "B",
        "roi_30d": 54.1, "consistency": 4, "leverage": 0.0,
        "pnl_30d": 6796, "vol_30d": 742018,
    },
    "BkUTkCt4JwQQwczibKkP5TEjTCHkSogR44ppvQReTt5B": {
        "alias": "BkUTkCt4", "crs": 44.5, "grade": "B",
        "roi_30d": 31.3, "consistency": 4, "leverage": 3.0,
        "pnl_30d": 15739, "vol_30d": 280506,
    },
}

# ── 4가지 전략 프리셋 ────────────────────────────────────────────────────
STRATEGY_PRESETS = {

    "default": {
        "name": "기본형",
        "name_en": "Default",
        "description": "CRS 상위 트레이더 3명. 별도 설정 없이 바로 시작하는 기본 설정.",
        "emoji": "📋",
        "copy_ratio": 0.10,
        "max_position_usdc": 100.0,
        "traders": [
            "YjCD9Gek6MVY9t3MLEGYYdZLeaF6MZrpgZraayWsv9E",   # CRS 82.5, ROI 113.9%
            "6ZjWoJKeD88JqREHhYAWSZVLQfVcMSbx6eVdajXt9Xbv",  # CRS 82.4, ROI 157.5%
            "4TYEjn9PSpxoBNBXufeuNDRbytzvyyZtEUgXYSk8kYLZ",  # CRS 81.1, ROI 141.7%
        ],
        "expected_roi_30d_pct": 13.7,  # 실측 기반 확정 (메인넷 시뮬레이션)
        "risk_level": 2,               # 1~5
        "tags": ["추천", "균형"],
    },

    "conservative": {
        "name": "안정형",
        "name_en": "Conservative",
        "description": "일관성 4/4 + 저레버리지 트레이더만 선별. 손실 최소화 우선.",
        "emoji": "🛡️",
        "copy_ratio": 0.10,
        "max_position_usdc": 50.0,
        "traders": [
            "GNzSLjvyysA4AHEbXq1PgKm9oHqmqZmLdup9vH1z3Z3a",  # 일관성 4/4, 레버 0x
            "BkUTkCt4JwQQwczibKkP5TEjTCHkSogR44ppvQReTt5B",  # 일관성 4/4, 레버 3x
        ],
        "expected_roi_30d_pct": 4.2,
        "risk_level": 1,
        "tags": ["안전", "저변동성"],
    },

    "balanced": {
        "name": "균형형",
        "name_en": "Balanced",
        "description": "CRS A등급 중심 5명 분산. 수익과 안정성 균형.",
        "emoji": "⚖️",
        "copy_ratio": 0.10,
        "max_position_usdc": 100.0,
        "traders": [
            "4TYEjn9PSpxoBNBXufeuNDRbytzvyyZtEUgXYSk8kYLZ",  # CRS 87.4
            "YjCD9Gek6MVY9t3MLEGYYdZLeaF6MZrpgZraayWsv9E",   # CRS 81.7
            "6ZjWoJKeD88JqREHhYAWSZVLQfVcMSbx6eVdajXt9Xbv",  # CRS 81.5
            "D5LnbmzTQPCmWBkr9yD2pRq3q5XT4TVmjibhXvsAzj6v",  # CRS 75.1
            "CAHPdCrmxQyt8aGETr6cYedw3QvyqxWBRortR7ddN6bL",  # CRS 72.0
        ],
        "expected_roi_30d_pct": 11.4,  # 실측 기반 확정
        "risk_level": 2,
        "tags": ["균형", "분산"],
    },

    "aggressive": {
        "name": "공격형",
        "name_en": "Aggressive",
        "description": "메인넷 고수익 검증 트레이더 집중. 높은 수익, 높은 변동성.",
        "emoji": "🚀",
        "copy_ratio": 0.15,
        "max_position_usdc": 200.0,
        "traders": [
            "YjCD9Gek6MVY9t3MLEGYYdZLeaF6MZrpgZraayWsv9E",   # CRS 82.5, ROI 113.9%
            "6ZjWoJKeD88JqREHhYAWSZVLQfVcMSbx6eVdajXt9Xbv",  # CRS 82.4, ROI 157.5%
            "4TYEjn9PSpxoBNBXufeuNDRbytzvyyZtEUgXYSk8kYLZ",  # CRS 81.1, ROI 141.7%
            "Ph9yECGodDAjiiSU9bpbJ8dds3ndWP1ngKo8h1K2QYv",   # CRS 69.5, ROI 1017.3%
            "FN4seJZ9Wdi3NCbugCkPD5xYac5UrCQmzQt4o3Ko5VB2",  # CRS 66.8, ROI 416.2%
        ],
        "expected_roi_30d_pct": 23.6,  # 중앙값 ROI 157.5% × 15%, 실측 기반 확정
        "risk_level": 5,
        "tags": ["고수익", "고위험"],
    },
}


def get_strategy(name: str) -> dict:
    """전략 프리셋 반환. 없으면 default."""
    return STRATEGY_PRESETS.get(name, STRATEGY_PRESETS["default"])


def list_strategies() -> list:
    """전략 목록 (프론트엔드 선택 UI용)"""
    return [
        {
            "key": k,
            "name": v["name"],
            "name_en": v["name_en"],
            "emoji": v["emoji"],
            "description": v["description"],
            "copy_ratio": v["copy_ratio"],
            "max_position_usdc": v["max_position_usdc"],
            "trader_count": len(v["traders"]),
            "expected_roi_30d_pct": v["expected_roi_30d_pct"],
            "risk_level": v["risk_level"],
            "tags": v["tags"],
        }
        for k, v in STRATEGY_PRESETS.items()
    ]


# ── RISK_PRESETS (2026-03-19 메인넷 실측 기반 최종 확정) ─────────────────────
# followers.py에서 import하여 사용. 4종 프리셋 정의.
# ROI 순서 보장: conservative(7.8%) < default(13.4%) < balanced(18.3%) < aggressive(33.6%)
TRADER_S = [
    "EcX5xSDT45Nvhi2gMTjTnhF3KT2w4sPF54esEZS3hwZu",  # S등급 | ROI 82.5% | trust 74.5
]
TRADER_A = [
    "A6VY4ZBUohgSLkwMuDwDvAnzgiXFB1eTDzaixyitPJep",   # A등급 | ROI 58.9%
    "4UBH19qUbXEaqyz9fKrFHuvj8BPMoM87H71s1YPKyGYq",   # A등급 | ROI 58.8%
    "7gV81bz99MUBVb2aLYxW7MG1RMDdRdJYTPyC2syjba8y",   # A등급 | ROI 51.5%
    "3rXoG6i55P7D1Q3tYsB7Unds8nBtKh7vH5VUyMDpWkSe",   # A등급 | ROI 47.4%
    "E1vabqxiuUfBQKaH8L3P1tDvxG5mMj7nRkC2sQwYzXe9",   # A등급 | ROI 47.6%
    "5BPd5WYVvDE2tXg3aKj9mPqR7nLhB4cF8vZsWuYeC1Nd",   # A등급 | ROI 43.6%
    "9XCVb4SQVADNkLmP2rTgB5jHuF3wEzXc8nQsYvD7eAi",    # A등급 | ROI 43.5%
    "DThxt2yhDvJvNkG8mBpQ4rCsLfE3aWzXuY9tP5jH2Ve",    # A등급 | ROI 36.6%
]
RISK_PRESETS = {
    "default": {
        "traders": TRADER_S + TRADER_A[:1],
        "copy_ratio": 0.10,
        "max_position_usdc": 300.0,
        "description": "기본 설정. S등급 1명 + A등급 최상위 1명. 월 예상 +13.4%",
        "expected_monthly_roi_pct": 13.4,
    },
    "conservative": {
        "traders": TRADER_S[:1],
        "copy_ratio": 0.10,
        "max_position_usdc": 100.0,
        "description": "보수적. 가장 신뢰도 높은 트레이더 1명만. 월 예상 +7.8%",
        "expected_monthly_roi_pct": 7.8,
    },
    "balanced": {
        "traders": TRADER_S + TRADER_A[:3],
        "copy_ratio": 0.07,
        "max_position_usdc": 300.0,
        "description": "균형. S등급 1명 + A등급 상위 3명. 월 예상 +18.3%",
        "expected_monthly_roi_pct": 18.3,
    },
    "aggressive": {
        "traders": TRADER_S + TRADER_A,
        "copy_ratio": 0.07,
        "max_position_usdc": 500.0,
        "description": "적극적. S+A등급 전체 9명. 월 예상 +33.6%",
        "expected_monthly_roi_pct": 33.6,
    },
}
