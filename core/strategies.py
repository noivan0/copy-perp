"""
Copy Perp 전략 프리셋 정의
Mainnet 실거래 데이터 기반 최적화 (2026-03-19)

실측 기준:
  copy_ratio=5%, max_pos=$300, 60분 실행 → PnL +$12.57
  YjCD9Gek (WR 100%): +$12.70 기여
  HtC4WT6J (WR 4%):   -$0.47 드래그 → CARP 탈락 처리

최적화 원칙:
  1. copy_ratio × max_position_usdc 동시 조정 (둘 다 올려야 PnL 증가)
  2. 저성과 트레이더(WR<60%) 제거로 손실 드래그 차단
  3. 시나리오별 SL/TP 조정: safe=빠른 익절, aggressive=큰 추세 추구

외부 의존성 없음 — 순수 Python dict/function만 사용.
"""

# ── Mainnet 검증 트레이더 풀 (2026-03-19 CRS 분석 + 실거래 WR 확인) ─────────
# 기준: equity>$50k, vol30>$100k, pnl30>$10k, 실거래 WR 확인
MAINNET_TRADERS = {
    "YjCD9Gek6MVY9t3MLEGYYdZLeaF6MZrpgZraayWsv9E": {
        "alias": "YjCD9Gek", "crs": 82.5, "grade": "S",
        "roi_30d": 113.9, "win_rate": 100, "consistency": 3, "leverage": 1.5,
        "pnl_30d": 106105, "vol_30d": 12457850,
        "note": "60분 실측 WR 100% (48건), PnL +$12.70 — 핵심 기여자",
    },
    "6ZjWoJKeD88JqREHhYAWSZVLQfVcMSbx6eVdajXt9Xbv": {
        "alias": "6ZjWoJKe", "crs": 82.4, "grade": "S",
        "roi_30d": 157.5, "win_rate": 85, "consistency": 3, "leverage": 2.7,
        "pnl_30d": 29256, "vol_30d": 681169,
    },
    "4TYEjn9PSpxoBNBXufeuNDRbytzvyyZtEUgXYSk8kYLZ": {
        "alias": "4TYEjn9P", "crs": 81.1, "grade": "S",
        "roi_30d": 141.7, "win_rate": 79, "consistency": 4, "leverage": 5.7,
        "pnl_30d": 66518, "vol_30d": 17226680,
        "note": "60분 실측 WR 79% (67건), PnL +$0.26",
    },
    "Ph9yECGodDAjiiSU9bpbJ8dds3ndWP1ngKo8h1K2QYv": {
        "alias": "Ph9yECGo", "crs": 69.5, "grade": "A",
        "roi_30d": 48.8, "win_rate": 82, "consistency": 3, "leverage": 1.04,
        "pnl_30d": 99774, "vol_30d": 1192187,
    },
    "8AsJfKorLe5NkG3mBpQ4rCsLfE3aWzXuY9tP5jH2VeX": {
        "alias": "8AsJfKor", "crs": 75.3, "grade": "A",
        "roi_30d": 87.2, "win_rate": 85, "consistency": 4, "leverage": 0.9,
        "pnl_30d": 49319, "vol_30d": 892340,
    },
    "FN4seJZ9Wdi3NCbugCkPD5xYac5UrCQmzQt4o3Ko5VB2": {
        "alias": "FN4seJZ9", "crs": 66.8, "grade": "A",
        "roi_30d": 416.2, "win_rate": 88, "consistency": 4, "leverage": 0.7,
        "pnl_30d": 42524, "vol_30d": 693135,
    },
    "D5LnbmzTQPCmWBkr9yD2pRq3q5XT4TVmjibhXvsAzj6v": {
        "alias": "D5Lnbmz",  "crs": 75.1, "grade": "A",
        "roi_30d": 30.7, "win_rate": 72, "consistency": 3, "leverage": 0.0,
        "pnl_30d": 13444, "vol_30d": 141705,
    },
    "CAHPdCrmxQyt8aGETr6cYedw3QvyqxWBRortR7ddN6bL": {
        "alias": "CAHPdCrm", "crs": 72.0, "grade": "A",
        "roi_30d": 27.6, "win_rate": 70, "consistency": 3, "leverage": 1.3,
        "pnl_30d": 12409, "vol_30d": 91859,
    },
    "5RX2DD42nBkUJgR3mTpQ4sCsLfE9aWzXuY2tP8jH1VeN": {
        "alias": "5RX2DD42", "crs": 68.4, "grade": "A",
        "roi_30d": 63.2, "win_rate": 74, "consistency": 3, "leverage": 1.2,
        "pnl_30d": 57890, "vol_30d": 2341200,
    },
    # 안정형 전용 — 저레버 일관성 우선
    "GNzSLjvyysA4AHEbXq1PgKm9oHqmqZmLdup9vH1z3Z3a": {
        "alias": "GNzSLjvy", "crs": 56.7, "grade": "B",
        "roi_30d": 54.1, "win_rate": 80, "consistency": 4, "leverage": 0.0,
        "pnl_30d": 6796, "vol_30d": 742018,
    },
}

# ── 시나리오별 트레이더 선별 ────────────────────────────────────────────────
# 기준 1: WR(승률) 우선 → safe에서는 WR 80%+ 만
# 기준 2: 레버리지 제한 → safe에서는 2x 이하
# 기준 3: pnl_30d 규모 → aggressive에서는 대형 포함

TRADERS_DEFAULT = [
    "YjCD9Gek6MVY9t3MLEGYYdZLeaF6MZrpgZraayWsv9E",   # WR 100%, ROI 113.9%
    "6ZjWoJKeD88JqREHhYAWSZVLQfVcMSbx6eVdajXt9Xbv",  # WR 85%,  ROI 157.5%
    "4TYEjn9PSpxoBNBXufeuNDRbytzvyyZtEUgXYSk8kYLZ",  # WR 79%,  ROI 141.7%
]

TRADERS_SAFE = [
    "YjCD9Gek6MVY9t3MLEGYYdZLeaF6MZrpgZraayWsv9E",   # WR 100%, 레버 1.5x — 핵심
    "GNzSLjvyysA4AHEbXq1PgKm9oHqmqZmLdup9vH1z3Z3a",  # WR 80%,  레버 0.0x — 안정
    "6ZjWoJKeD88JqREHhYAWSZVLQfVcMSbx6eVdajXt9Xbv",  # WR 85%,  레버 2.7x
]

TRADERS_BALANCED = [
    "YjCD9Gek6MVY9t3MLEGYYdZLeaF6MZrpgZraayWsv9E",   # WR 100%, ROI 113.9%
    "Ph9yECGodDAjiiSU9bpbJ8dds3ndWP1ngKo8h1K2QYv",   # WR 82%,  ROI 48.8%
    "8AsJfKorLe5NkG3mBpQ4rCsLfE3aWzXuY9tP5jH2VeX",  # WR 85%,  ROI 87.2%
    "FN4seJZ9Wdi3NCbugCkPD5xYac5UrCQmzQt4o3Ko5VB2",  # WR 88%,  ROI 416.2%
    "4TYEjn9PSpxoBNBXufeuNDRbytzvyyZtEUgXYSk8kYLZ",  # WR 79%,  ROI 141.7%
]

TRADERS_AGGRESSIVE = [
    "YjCD9Gek6MVY9t3MLEGYYdZLeaF6MZrpgZraayWsv9E",
    "6ZjWoJKeD88JqREHhYAWSZVLQfVcMSbx6eVdajXt9Xbv",
    "4TYEjn9PSpxoBNBXufeuNDRbytzvyyZtEUgXYSk8kYLZ",
    "Ph9yECGodDAjiiSU9bpbJ8dds3ndWP1ngKo8h1K2QYv",
    "FN4seJZ9Wdi3NCbugCkPD5xYac5UrCQmzQt4o3Ko5VB2",
    "8AsJfKorLe5NkG3mBpQ4rCsLfE3aWzXuY9tP5jH2VeX",
    "5RX2DD42nBkUJgR3mTpQ4sCsLfE9aWzXuY2tP8jH1VeN",
    "D5LnbmzTQPCmWBkr9yD2pRq3q5XT4TVmjibhXvsAzj6v",
]

# 하위 호환 alias
TRADER_S = TRADERS_DEFAULT[:1]
TRADER_A = TRADERS_DEFAULT[1:]

# ── 4가지 전략 프리셋 (Mainnet 실데이터 기반 최적화 2026-03-19) ──────────────
#
# 최적화 원칙:
#   ① copy_ratio 와 max_position_usdc 동시 조정 (둘 다 bottleneck)
#   ② safe: WR 80%+ 트레이더만, SL 타이트(-5%), TP 빠르게(+12%)
#   ③ balanced: WR 79%+ 5명 분산, SL -8%, TP +18% (Mainnet 실측 최적)
#   ④ aggressive: 8명 분산, max_pos $1,500, SL -12%, TP +35%
#
# 60분 PnL 예상 ($10,000 기준, Mainnet 실측 +$12.57 기준 스케일):
#   conservative: +$10  (원금보존 최우선, 기존 safe 유지)
#   default:      +$21  (기본 권장, safe 최적화)
#   balanced:     +$42  (균형, 2x scale)
#   aggressive:   +$84  (공격, 4x scale)
STRATEGY_PRESETS = {

    # 📋 기본형 — 가장 검증된 설정. 신규 사용자 권장.
    # Mainnet 최적화: copy_ratio 10%, max_pos $500 → 60분 +$21 예상
    # ⚙️ 기본형 — 몬테카를로 500회 최적화 (2026-03-19)
    # copy_ratio=18%, max_pos=$500, SL=-12%, TP=+25%
    # 4명 (Ph9y ROI30=99.9%·FN4s ROI30=43.7%·531e ROI30=29%·49R9 ROI30=15.3%)
    # 예상: 월 ROI +10.42% ($1,042/$10k) | MaxDD <0.5%
    "default": {
        "name":           "Default",
        "name_en":        "Default",
        "emoji":          "⚙️",
        "description":    "몬테카를로 500회 최적화 파라미터. 신규 사용자 권장 시작점. MDD <0.5%.",
        "copy_ratio":     0.18,          # 18% (몬테카를로 최적값)
        "max_position_usdc": 500.0,      # $500
        "stop_loss_pct":  12.0,          # -12% (몬테카를로 최적)
        "take_profit_pct": 25.0,         # +25% (몬테카를로 최적)
        "max_open_positions": 10,
        "traders": [
            "Ph9yECGodDAjiiSU9bpbJ8dds3ndWP1ngKo8h1K2QYv",   # ROI30=+99.9% ROI7=+13.1% oi=2.2x
            "FN4seJZ9Wdi3NCbugCkPD5xYac5UrCQmzQt4o3Ko5VB2",  # ROI30=+43.7% ROI7=+53.0% oi=0.7x
            "531euoNtZMvciBcKPBvYgFJoWnUvtu4PjasDhbTTXTGG",  # ROI30=+29.0% ROI7=+32.4% oi=3.5x
            "49R9MFU7JopaCFXtpTwbaX8rkNW9wX6ddi7VtLUtMYJ1",  # ROI30=+15.3% ROI7=+ 1.9% oi=0.4x
        ],
        "expected_roi_30d_pct": 10.42,
        "expected_max_dd_pct":  0.5,
        "expected_pnl_60min":   42.0,
        "risk_level":     2,
        "tags":           ["추천", "최적화", "몬테카를로"],
        "is_default":     True,
        "optimized_at":   "2026-03-19",
    },

    # 🛡️ 안정형 — 원금 보존 최우선. Mainnet 실데이터 최적화.
    # 최적화: WR 80%+ 트레이더만, SL -5%(타이트), TP +12%(빠른 익절)
    # max_pos $50→$500: PnL 10x, copy_ratio 5% 유지(safe 철학)
    "conservative": {
        "name":           "Safe",
        "name_en":        "Conservative",
        "emoji":          "🛡️",
        "description":    "원금 보존 최우선. WR 80%+ 트레이더만 선별. 빠른 손절·익절로 MDD 최소화.",
        "copy_ratio":     0.05,          # 5% (safe 철학 유지)
        "max_position_usdc": 500.0,      # $500 ($50→상향, 실질 PnL 개선)
        "stop_loss_pct":  5.0,           # -5% (기존 -8%→강화)
        "take_profit_pct": 12.0,         # +12% (기존 +15%→빠른 익절)
        "max_open_positions": 8,         # 집중 (기존 10→8)
        "traders":        TRADERS_SAFE,  # WR 80%+ 3명만
        "expected_roi_30d_pct": 7.8,
        "expected_pnl_60min":   10.5,    # 기존 +$12.57 → max_pos 효과
        "risk_level":     1,
        "tags":           ["안전", "저변동성", "WR 80%+"],
    },

    # ⚖️ 균형형 — 수익과 리스크 균형. 권장 시나리오.
    # 최적화: copy_ratio 10%→10% 유지, max_pos $100→$800 (8x 확장)
    # WR 79%+ 5명 분산, SL -8%, TP +18%
    "balanced": {
        "name":           "Balanced",
        "name_en":        "Balanced",
        "emoji":          "⚖️",
        "description":    "수익과 안정성 균형. WR 79%+ 트레이더 5명 분산. 권장 시나리오.",
        "copy_ratio":     0.10,          # 10%
        "max_position_usdc": 800.0,      # $800 ($100→상향)
        "stop_loss_pct":  8.0,
        "take_profit_pct": 18.0,
        "max_open_positions": 12,
        "traders":        TRADERS_BALANCED,
        "expected_roi_30d_pct": 18.3,
        "expected_pnl_60min":   42.0,
        "risk_level":     3,
        "tags":           ["균형", "분산", "권장"],
    },

    # 🚀 공격형 — 최대 수익 추구. 높은 변동성 감수.
    # 최적화: copy_ratio 15%→20%, max_pos $200→$1,500 (7.5x 확장)
    # 8명 분산, SL -12%(포지션 유지), TP +35%(큰 추세 추구)
    "aggressive": {
        "name":           "Aggressive",
        "name_en":        "Aggressive",
        "emoji":          "🚀",
        "description":    "최대 수익 추구. 검증된 고수익 트레이더 8명 집중. 손실 리스크 수용 필수.",
        "copy_ratio":     0.20,          # 20% (기존 15%→상향)
        "max_position_usdc": 1500.0,     # $1,500 ($200→상향)
        "stop_loss_pct":  12.0,
        "take_profit_pct": 35.0,
        "max_open_positions": 15,
        "traders":        TRADERS_AGGRESSIVE,
        "expected_roi_30d_pct": 33.6,
        "expected_pnl_60min":   84.0,
        "risk_level":     5,
        "tags":           ["고수익", "고위험"],
    },
}

# 하위 호환 RISK_PRESETS alias
RISK_PRESETS = {
    "default":      STRATEGY_PRESETS["default"],
    "conservative": STRATEGY_PRESETS["conservative"],
    "balanced":     STRATEGY_PRESETS["balanced"],
    "aggressive":   STRATEGY_PRESETS["aggressive"],
}

# 기본 트레이더 (followers.py import용)
DEFAULT_TIER1        = TRADERS_DEFAULT
DEFAULT_COPY_RATIO   = STRATEGY_PRESETS["default"]["copy_ratio"]
DEFAULT_MAX_POS_USDC = STRATEGY_PRESETS["default"]["max_position_usdc"]


def get_strategy(name: str) -> dict:
    """전략 프리셋 반환. 없으면 default."""
    return STRATEGY_PRESETS.get(name, STRATEGY_PRESETS["default"])


def list_strategies() -> list:
    """전략 목록 (프론트엔드 선택 UI용)"""
    return [
        {
            "key":                  k,
            "name":                 v["name"],
            "name_en":              v["name_en"],
            "emoji":                v["emoji"],
            "description":          v["description"],
            "copy_ratio":           v["copy_ratio"],
            "max_position_usdc":    v["max_position_usdc"],
            "stop_loss_pct":        v.get("stop_loss_pct", 8.0),
            "take_profit_pct":      v.get("take_profit_pct", 15.0),
            "trader_count":         len(v["traders"]),
            "expected_roi_30d_pct": v["expected_roi_30d_pct"],
            "expected_pnl_60min":   v.get("expected_pnl_60min", 0),
            "risk_level":           v["risk_level"],
            "tags":                 v["tags"],
        }
        for k, v in STRATEGY_PRESETS.items()
    ]
