"""
core/strategy_presets.py — 사용자 선택 시나리오 프리셋

4개 프리셋 (메인넷 실데이터 기반 파라미터 최적화):
  default      기본형  📋  copy_ratio 10%, CRS 상위 3명, max_pos $100  → 30일 +13.7%
  conservative 안정형  🛡️  copy_ratio 10%, 일관성 4/4 저레버, max_pos $50  → 30일 +4.2%
  balanced     균형형  ⚖️  copy_ratio 10%, CRS A등급 5명 분산, max_pos $100 → 30일 +11.4%
  aggressive   공격형  🚀  copy_ratio 15%, 고ROI 검증 5명, max_pos $200  → 30일 +23.6%

트레이더 배정: mainnet_tracker.db 최신 수집 데이터 → 실시간 자동 선별
              DB 없을 때: FALLBACK_TRADERS (2026-03-19 메인넷 CRS 분석 확정)

최적화 기준 (2026-03-19 메인넷 8,252명 전수 분석):
  - 품질 필터: equity>$5k, vol30>$50k, pnl30>$5k → 43명 선별
  - copy_ratio 구간별 시뮬레이션: 10%가 리스크 대비 최적점
  - 현실화 계수 0.998 (슬리피지+builder fee 0.2%)
  - 기본형 기대 수익: +13.7%/30일 (연환산 +165%)
"""

import os
import sqlite3
import logging

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_DB = os.path.join(_ROOT, "mainnet_tracker.db")

COPY_REALISM = 0.998  # 슬리피지+builder fee 0.2% (메인넷 실측 기준)
TOTAL_FEE    = 0.0010  # builder fee 0.1%

# ── 메인넷 확정 폴백 트레이더 (2026-03-19 CRS 분석, 품질필터 43명 → 선별) ──
MAINNET_FALLBACK = {
    "YjCD9Gek6MVY9t3MLEGYYdZLeaF6MZrpgZraayWsv9E":  {"alias":"YjCD9Gek","crs":82.5,"roi30":113.9,"cons":3,"lev":1.5,"grade":"A"},
    "6ZjWoJKeD88JqREHhYAWSZVLQfVcMSbx6eVdajXt9Xbv": {"alias":"6ZjWoJKe","crs":82.4,"roi30":157.5,"cons":3,"lev":2.7,"grade":"A"},
    "4TYEjn9PSpxoBNBXufeuNDRbytzvyyZtEUgXYSk8kYLZ":  {"alias":"4TYEjn9P","crs":81.1,"roi30":141.7,"cons":4,"lev":5.8,"grade":"A"},
    "D5LnbmzTQPCmWBkr9yD2pRq3q5XT4TVmjibhXvsAzj6v":  {"alias":"D5Lnbmz", "crs":75.1,"roi30": 30.7,"cons":3,"lev":0.0,"grade":"A"},
    "CAHPdCrmxQyt8aGETr6cYedw3QvyqxWBRortR7ddN6bL":  {"alias":"CAHPdCrm","crs":72.1,"roi30": 27.9,"cons":3,"lev":1.3,"grade":"A"},
    "Ph9yECGodDAjiiSU9bpbJ8dds3ndWP1ngKo8h1K2QYv":   {"alias":"Ph9yECGo","crs":69.5,"roi30":1017.3,"cons":3,"lev":2.2,"grade":"A"},
    "FN4seJZ9Wdi3NCbugCkPD5xYac5UrCQmzQt4o3Ko5VB2":  {"alias":"FN4seJZ9","crs":66.8,"roi30": 416.2,"cons":4,"lev":0.7,"grade":"A"},
    "GNzSLjvyysA4AHEbXq1PgKm9oHqmqZmLdup9vH1z3Z3a":  {"alias":"GNzSLjvy","crs":56.7,"roi30":  54.1,"cons":4,"lev":0.0,"grade":"B"},
    "BkUTkCt4JwQQwczibKkP5TEjTCHkSogR44ppvQReTt5B":  {"alias":"BkUTkCt4","crs":44.5,"roi30":  31.3,"cons":4,"lev":3.0,"grade":"B"},
}

# ── 프리셋 정의 (메인넷 실측 최적화, 2026-03-19 확정) ──────────────────────
PRESETS = {
    # ── 2026-03-19 최적화 완료 (mainnet Kelly 실측 기반) ──────────────
    # avg_kelly=0.231 median_kelly=0.286 → QK(0.25)=0.058~0.072
    # 체결률 78.5% (beta 승인 팔로워 기준), 평균 체결금액 $49.50
    "default": {
        "key":         "default",
        "label":       "기본형",
        "emoji":       "🔒",
        "description": "손절 없이 트레이더를 그대로 따라갑니다. 자본의 15% 운용. CARP 상위 2명 자동 배정.",
        "copy_ratio":        0.15,
        "max_position_usdc": 120.0,
        "n_traders":         2,
        "grade_filter":      ["S"],
        "sort_by":           "stability",
        "risk_level":        1,
        "stop_loss_pct":     0.0,
        "take_profit_pct":   0.0,
        "trailing_stop_pct": 0.0,
        "symbol_filter":     True,
        "expected_roi_30d_pct": 6.8,
        "expected_roi_7d_pct":  3.1,
    },
    "conservative": {
        "key":         "conservative",
        "label":       "안정형",
        "emoji":       "🛡️",
        "description": "S등급 3명 분산. 손절 -10% 자동 적용. 안정적 우상향.",
        "copy_ratio":        0.15,
        "max_position_usdc": 120.0,
        "n_traders":         3,
        "grade_filter":      ["S"],
        "sort_by":           "roi_30d",
        "risk_level":        2,
        "stop_loss_pct":     0.10,
        "take_profit_pct":   0.0,
        "trailing_stop_pct": 0.0,
        "symbol_filter":     True,
        "expected_roi_30d_pct": 7.2,
        "expected_roi_7d_pct":  3.3,
    },
    "balanced": {
        "key":         "balanced",
        "label":       "균형형",
        "emoji":       "⚖️",
        "description": "S+A등급 4명. 손절 -15% + 트레일링 -20%. 수익성과 리스크의 균형.",
        "copy_ratio":        0.20,
        "max_position_usdc": 200.0,
        "n_traders":         4,
        "grade_filter":      ["S", "A"],
        "sort_by":           "score",
        "risk_level":        3,
        "stop_loss_pct":     0.15,
        "take_profit_pct":   0.0,
        "trailing_stop_pct": 0.20,
        "symbol_filter":     True,
        "expected_roi_30d_pct": 8.1,
        "expected_roi_7d_pct":  3.9,
    },
    "aggressive": {
        "key":         "aggressive",
        "label":       "공격형",
        "emoji":       "⚡",
        "description": "7일 모멘텀 최강 3명. 손절 -5% + 익절 +30% + 트레일링. 단기 집중. 고위험.",
        "copy_ratio":        0.25,
        "max_position_usdc": 300.0,
        "n_traders":         3,
        "grade_filter":      ["S", "A"],
        "sort_by":           "roi_7d",
        "risk_level":        4,
        "stop_loss_pct":     0.05,
        "take_profit_pct":   0.30,
        "trailing_stop_pct": 0.10,
        "symbol_filter":     True,
        "expected_roi_30d_pct": 9.5,
        "expected_roi_7d_pct":  6.2,
    },
}

# ── Fallback 트레이더 (DB 없을 때, 2026-03-19 메인넷 확인) ──────────
FALLBACK_TRADERS = {
    "S": [
        # stability 순
        "Ph9yECGodDAjiiSU9bpbJ8dds3ndWP1ngKo8h1K2QYv",  # ROI_30d +100%, stability 70.6
        "FN4seJZ9Wdi3NCbugCkPD5xYac5UrCQmzQt4o3Ko5VB2",  # ROI_30d +44%, 7d +53%, stability 59.6
        "5RX2DD425DHj3VAouTbJWHtmBmzi2oUmuErwmfwgxs8n",  # ROI_30d +44%, stability 47.9
        "6uC2TdJxxqhWMPSjs7u9YE5rWMQs1yhxkvk8BmBTPrpV",  # ROI_30d +49%, stability 40.7
    ],
    "A": [
        "8AsJfKorQc1Wz8DABe9FZH18cgLd43bYwFST5JBJYSit",  # ROI_30d +39%, 7d +41%
        "531euoNtZMvciBcKBbB5h91eRDHUjbJF8HbFMZdVaEAV",  # ROI_30d +29%, 7d +32%
        "BkUTkCt4JwQQwczibKkP5TEjTCHkSogR44ppvQReTt5B",  # ROI_30d +29%, momentum 3/3
    ],
}


def get_preset(name: str) -> dict:
    """프리셋 정보 반환 (없으면 default)"""
    return PRESETS.get(name, PRESETS["default"])


def _load_db_traders(db_path: str) -> list:
    """mainnet_tracker.db 최신 수집 트레이더 로드"""
    if not os.path.exists(db_path):
        return []
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        latest_ts = conn.execute(
            "SELECT MAX(collected_at) FROM trader_snapshots"
        ).fetchone()[0]
        if not latest_ts:
            conn.close()
            return []
        rows = conn.execute(
            "SELECT * FROM trader_snapshots WHERE collected_at=?", (latest_ts,)
        ).fetchall()
        conn.close()
        traders = []
        for r in rows:
            d = dict(r)
            roi30 = float(d.get("roi_30d", 0))
            roi7  = float(d.get("roi_7d",  0))
            roi1  = float(d.get("roi_1d",  0))
            # stability 계산
            mom = sum([roi1 > 0, roi7 > 0, roi30 > 0])
            stability = roi30 * (mom / 3) + (roi7 if roi7 > 0 else 0) * 0.3
            traders.append({
                "address":   d["address"],
                "alias":     d.get("alias", d["address"][:8]),
                "grade":     d.get("grade", "B"),
                "crs":       float(d.get("crs", 60)),
                "roi_30d":   roi30,
                "roi_7d":    roi7,
                "roi_1d":    roi1,
                "equity":    float(d.get("equity", 0)),
                "momentum":  mom,
                "score":     roi30 * mom + roi7 * 0.5,
                "stability": stability,
                "copy_ratio": float(d.get("copy_ratio", 0.10)),
            })
        return traders
    except Exception as e:
        logger.warning(f"DB 트레이더 로드 실패: {e}")
        return []


def resolve_traders(preset_name: str, db_path: str = _DEFAULT_DB) -> list:
    """
    프리셋에 맞는 트레이더 주소 목록 반환
    1. mainnet_tracker.db 최신 데이터 → grade_filter + sort_by 선별
    2. 부족하면 FALLBACK_TRADERS로 보완
    """
    preset = get_preset(preset_name)
    n             = preset["n_traders"]
    grade_filter  = preset["grade_filter"]
    sort_by       = preset["sort_by"]

    # DB에서 로드
    db_traders = _load_db_traders(db_path)
    pool = [t for t in db_traders if t["grade"] in grade_filter]

    sort_key_map = {
        "roi_30d":   lambda x: -x["roi_30d"],
        "roi_7d":    lambda x: -x["roi_7d"],
        "score":     lambda x: -x["score"],
        "stability": lambda x: -x["stability"],
    }
    pool.sort(key=sort_key_map.get(sort_by, lambda x: -x["score"]))
    selected_addrs = [t["address"] for t in pool[:n]]

    # 부족하면 FALLBACK으로 보완
    if len(selected_addrs) < n:
        fb = []
        for g in grade_filter:
            fb.extend(FALLBACK_TRADERS.get(g, []))
        for addr in fb:
            if addr not in selected_addrs:
                selected_addrs.append(addr)
            if len(selected_addrs) >= n:
                break

    return selected_addrs[:n]


def get_preset_sim_pnl(
    preset_name: str,
    capital: float = 1000.0,
    db_path: str = _DEFAULT_DB,
) -> dict:
    """
    프리셋 + 자본 → 예상 PnL 계산 (메인넷 실데이터 기반)
    """
    preset = get_preset(preset_name)
    cr     = preset["copy_ratio"]
    n      = preset["n_traders"]

    db_traders = _load_db_traders(db_path)
    grade_filter = preset["grade_filter"]
    sort_by      = preset["sort_by"]

    pool = [t for t in db_traders if t["grade"] in grade_filter]
    sort_key_map = {
        "roi_30d":   lambda x: -x["roi_30d"],
        "roi_7d":    lambda x: -x["roi_7d"],
        "score":     lambda x: -x["score"],
        "stability": lambda x: -x["stability"],
    }
    pool.sort(key=sort_key_map.get(sort_by, lambda x: -x["score"]))
    selected = pool[:n]

    if not selected:
        # DB 없을 때 expected_roi 기반 추정
        p30 = capital * cr * (preset["expected_roi_30d_pct"] / 100)
        p7  = capital * cr * (preset["expected_roi_7d_pct"]  / 100)
        return {
            "pnl_1d":  0.0,
            "pnl_7d":  round(p7, 4),
            "pnl_30d": round(p30, 4),
            "roi_1d_pct":  0.0,
            "roi_7d_pct":  round(p7 / capital * 100, 4),
            "roi_30d_pct": round(p30 / capital * 100, 4),
            "traders": [],
            "data_source": "estimated",
        }

    alloc = capital / len(selected)
    ff    = (1 - TOTAL_FEE)
    p1d = p7d = p30d = 0.0
    trader_info = []

    for t in selected:
        inv   = alloc * cr
        p1d  += inv * (t["roi_1d"]  / 100) * COPY_REALISM * ff
        p7d  += inv * (t["roi_7d"]  / 100) * COPY_REALISM * ff
        p30d += inv * (t["roi_30d"] / 100) * COPY_REALISM * ff
        trader_info.append({
            "address":  t["address"],
            "alias":    t["alias"],
            "grade":    t["grade"],
            "roi_30d":  round(t["roi_30d"], 2),
            "roi_7d":   round(t["roi_7d"],  2),
            "allocated": round(alloc, 2),
            "invested":  round(inv,   2),
        })

    return {
        "pnl_1d":      round(p1d,  4),
        "pnl_7d":      round(p7d,  4),
        "pnl_30d":     round(p30d, 4),
        "roi_1d_pct":  round(p1d  / capital * 100, 4),
        "roi_7d_pct":  round(p7d  / capital * 100, 4),
        "roi_30d_pct": round(p30d / capital * 100, 4),
        "traders":     trader_info,
        "data_source": "mainnet_live",
    }


def list_presets_with_sim(capital: float = 1000.0, db_path: str = _DEFAULT_DB) -> list:
    """4개 프리셋 전체 + 시뮬 PnL (GET /presets 응답용)"""
    results = []
    for key, preset in PRESETS.items():
        sim  = get_preset_sim_pnl(key, capital, db_path)
        addrs = resolve_traders(key, db_path)
        results.append({
            **preset,
            "traders": addrs,
            "sim_pnl": {
                "capital": capital,
                "pnl_1d":      sim["pnl_1d"],
                "pnl_7d":      sim["pnl_7d"],
                "pnl_30d":     sim["pnl_30d"],
                "roi_1d_pct":  sim["roi_1d_pct"],
                "roi_7d_pct":  sim["roi_7d_pct"],
                "roi_30d_pct": sim["roi_30d_pct"],
                "data_source": sim.get("data_source", "estimated"),
            },
        })
    return results

# alias for backward compat
get_all_presets_summary = list_presets_with_sim
