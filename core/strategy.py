"""
Copy Perp 전략 프리셋 시스템
────────────────────────────────────────────────────
4가지 전략:
  PASSIVE  — 기본형 (안전, 그대로 따라가기)
  BALANCED — 균형형 (Kelly 비율 + 분산 복사)
  ALPHA    — 공격형 (고수익 Tier A 집중 + Kelly 최대화)
  TURTLE   — 손절형 (엄격한 손절 + 트레일링 스탑)

mainnet_stats 실데이터 기반으로 트레이더 자동 선택
"""

from __future__ import annotations

import math
import logging
from dataclasses import dataclass, field
from typing import Optional
import sqlite3
import os

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "copy_perp.db")

# ── 지원 심볼 (Pacifica mainnet 실측 확인 — 2026-03-19) ─────────────
# mainnet_trades + copy_trades 실데이터 기반 63개 실사용 종목
# FX 중 USDJPY 제외 (422 에러 확인), EURUSD는 포함 (미검증)
SUPPORTED_SYMBOLS: set[str] = {
    # Crypto
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK",
    "LTC", "UNI", "AAVE", "ARB", "NEAR", "SUI", "HYPE", "PUMP",
    "PENGU", "PAXG", "TAO", "XMR", "ZEC", "JUP", "ZRO", "TRUMP",
    "FARTCOIN", "WIF", "WLD", "VIRTUAL", "ENA", "CRV", "LDO", "ICP",
    "BCH", "TON", "ASTER", "MON", "PIPPIN", "URNM", "2Z", "XPL",
    "ZK", "STRK", "LINEA", "ZORA", "LIT", "MEGA", "WLFI",
    "kBONK", "kPEPE",
    # Commodities (Pacifica perp)
    "CL", "XAG", "XAU", "NATGAS", "COPPER",
    # Stocks (Pacifica perp)
    "TSLA", "NVDA", "PLTR", "GOOGL", "HOOD", "BP",
    # FX (주의: USDJPY는 차단, 나머지는 실거래 확인 필요)
    "EURUSD",
    # 기타
    "PAXG",
}

# ── 전략 ID ───────────────────────────────────────────────
STRATEGY_PASSIVE  = "passive"    # 기본형 — 안전, 그대로 따라가기
STRATEGY_BALANCED = "balanced"   # 균형형 — Kelly + 분산
STRATEGY_ALPHA    = "alpha"      # 공격형 — Tier A 집중, Kelly 최대
STRATEGY_TURTLE   = "turtle"     # 손절형 — 보수적 손절 + 트레일링

ALL_STRATEGIES = [STRATEGY_PASSIVE, STRATEGY_BALANCED, STRATEGY_ALPHA, STRATEGY_TURTLE]


@dataclass
class StrategyConfig:
    """전략별 파라미터 완전 명세"""
    strategy_id:   str

    # 복사 비율
    copy_ratio:    float     # 트레이더 수량 대비 복사 비율
    use_kelly:     bool      # True → Kelly f* 자동 계산 (copy_ratio를 상한선으로)
    kelly_fraction: float    # 실전 Kelly 축소 계수 (0.25 = 쿼터 Kelly)
    max_position_usdc: float # 단일 포지션 최대 USDC

    # 손절 / 익절
    stop_loss_pct:    float  # 0.0 = 없음, 0.10 = 진입가 -10% 손절
    take_profit_pct:  float  # 0.0 = 없음, 0.30 = +30% 익절
    trailing_stop_pct: float # 0.0 = 없음, 0.15 = 고점 대비 -15% 트레일링

    # 트레이더 선택 기준
    min_carp:      float     # 최소 CARP 점수
    min_kelly:     float     # 최소 Kelly
    min_pf:        float     # 최소 Profit Factor
    min_cnt:       int       # 최소 거래 건수
    max_traders:   int       # 최대 팔로우 트레이더 수

    # 심볼 필터
    symbol_whitelist: set[str] = field(default_factory=lambda: SUPPORTED_SYMBOLS)

    # 설명
    label: str = ""
    description: str = ""


# ── 전략 프리셋 정의 ──────────────────────────────────────
STRATEGY_PRESETS: dict[str, StrategyConfig] = {

    STRATEGY_PASSIVE: StrategyConfig(
        strategy_id        = STRATEGY_PASSIVE,
        label              = "기본형 (Passive)",
        description        = "트레이더를 그대로 따라갑니다. 손절 없음. 안전한 소액 복사.",
        copy_ratio         = 0.10,    # 10% (기존 5% → 최소금액 미달 해소)
        use_kelly          = False,
        kelly_fraction     = 0.25,
        max_position_usdc  = 100.0,
        stop_loss_pct      = 0.0,
        take_profit_pct    = 0.0,
        trailing_stop_pct  = 0.0,
        min_carp           = 0.0,    # 제한 없음
        min_kelly          = 0.0,
        min_pf             = 1.0,
        min_cnt            = 0,
        max_traders        = 2,
    ),

    STRATEGY_BALANCED: StrategyConfig(
        strategy_id        = STRATEGY_BALANCED,
        label              = "균형형 (Balanced)",
        description        = "Kelly 비율로 최적 분산. CARP 상위 트레이더 자동 선택. 10% 손절.",
        copy_ratio         = 0.20,    # Kelly 상한선 20%
        use_kelly          = True,
        kelly_fraction     = 0.25,    # 쿼터 Kelly — 안전
        max_position_usdc  = 200.0,
        stop_loss_pct      = 0.10,    # -10% 손절
        take_profit_pct    = 0.0,
        trailing_stop_pct  = 0.0,
        min_carp           = 20.0,
        min_kelly          = 0.05,
        min_pf             = 1.5,
        min_cnt            = 30,
        max_traders        = 3,
    ),

    STRATEGY_ALPHA: StrategyConfig(
        strategy_id        = STRATEGY_ALPHA,
        label              = "공격형 (Alpha)",
        description        = "Mainnet CARP 최상위 트레이더만. Kelly 하프로 수익 극대화.",
        copy_ratio         = 0.35,    # Kelly 상한선 35%
        use_kelly          = True,
        kelly_fraction     = 0.50,    # 하프 Kelly
        max_position_usdc  = 500.0,
        stop_loss_pct      = 0.15,    # -15% 손절
        take_profit_pct    = 0.0,
        trailing_stop_pct  = 0.20,   # 고점 -20% 트레일링
        min_carp           = 55.0,
        min_kelly          = 0.30,
        min_pf             = 5.0,
        min_cnt            = 40,
        max_traders        = 1,       # 최고 1명만
    ),

    STRATEGY_TURTLE: StrategyConfig(
        strategy_id        = STRATEGY_TURTLE,
        label              = "손절형 (Turtle)",
        description        = "고승률 트레이더 + 엄격한 손절 + 트레일링. MDD 최소화.",
        copy_ratio         = 0.15,
        use_kelly          = True,
        kelly_fraction     = 0.25,
        max_position_usdc  = 150.0,
        stop_loss_pct      = 0.05,    # -5% 손절 (빠른 컷)
        take_profit_pct    = 0.25,    # +25% 익절
        trailing_stop_pct  = 0.08,   # 고점 -8% 트레일링
        min_carp           = 15.0,
        min_kelly          = 0.25,    # 고Kelly 필요
        min_pf             = 2.0,
        min_cnt            = 30,
        max_traders        = 2,
    ),
}


def get_preset(strategy_id: str) -> StrategyConfig:
    """전략 ID → StrategyConfig 반환. 없으면 PASSIVE 반환."""
    return STRATEGY_PRESETS.get(strategy_id, STRATEGY_PRESETS[STRATEGY_PASSIVE])


def select_traders_for_strategy(config: StrategyConfig) -> list[dict]:
    """
    mainnet_stats에서 전략 기준에 맞는 트레이더 자동 선택
    기준: CARP ≥ min_carp, Kelly ≥ min_kelly, PF ≥ min_pf, cnt ≥ min_cnt
    정렬: CARP × Kelly (복합 점수) 내림차순
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT trader_alias, trader_address,
                   pnl_all_time, pnl_30d, equity,
                   closed_cnt, win_rate, profit_factor,
                   payoff_ratio, kelly, avg_hold_min,
                   carp_score, tier
            FROM mainnet_stats
            WHERE carp_score >= ?
              AND kelly >= ?
              AND profit_factor >= ?
              AND closed_cnt >= ?
              AND pnl_all_time >= 0
            ORDER BY (carp_score * kelly) DESC
            LIMIT ?
        """, (
            config.min_carp, config.min_kelly,
            config.min_pf, config.min_cnt,
            config.max_traders,
        ))
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        logger.warning(f"트레이더 선택 오류: {e}")
        return []


def calc_kelly_ratio(config: StrategyConfig, trader: dict) -> float:
    """
    Kelly f* = wr - (1-wr)/payoff
    실전: kelly_fraction × f* (쿼터/하프 Kelly)
    copy_ratio를 상한선으로 클램핑
    """
    if not config.use_kelly:
        return config.copy_ratio

    wr      = float(trader.get("win_rate", 0)) / 100
    payoff  = float(trader.get("payoff_ratio", 0))
    if payoff <= 0:
        return config.copy_ratio * 0.5  # 기본값 절반

    raw_kelly = wr - (1 - wr) / payoff
    if raw_kelly <= 0:
        return 0.0  # 음의 기대값 → 거래 안 함

    fractional = raw_kelly * config.kelly_fraction
    # copy_ratio를 상한선으로
    return min(fractional, config.copy_ratio)


def is_symbol_supported(symbol: str, config: StrategyConfig) -> bool:
    """심볼이 전략의 화이트리스트에 있는지 확인"""
    return symbol.upper() in config.symbol_whitelist


def calc_stop_price(
    entry_price: float,
    side: str,           # 'bid' = long, 'ask' = short
    config: StrategyConfig,
) -> tuple[Optional[float], Optional[float]]:
    """
    (stop_loss_price, take_profit_price) 계산
    side: 'bid' = long (entry↑ 수익), 'ask' = short (entry↓ 수익)
    """
    if entry_price <= 0:
        return None, None

    is_long = (side == "bid")

    sl = None
    tp = None

    if config.stop_loss_pct > 0:
        sl = entry_price * (1 - config.stop_loss_pct) if is_long else entry_price * (1 + config.stop_loss_pct)

    if config.take_profit_pct > 0:
        tp = entry_price * (1 + config.take_profit_pct) if is_long else entry_price * (1 - config.take_profit_pct)

    return sl, tp


def should_stop(
    entry_price: float,
    current_price: float,
    high_price: float,       # 포지션 오픈 이후 최고가
    side: str,
    config: StrategyConfig,
) -> tuple[bool, str]:
    """
    손절/익절/트레일링 스탑 조건 확인
    반환: (청산 여부, 이유)
    """
    if entry_price <= 0 or current_price <= 0:
        return False, ""

    is_long = (side == "bid")

    # 현재 수익률
    if is_long:
        roi = (current_price - entry_price) / entry_price
    else:
        roi = (entry_price - current_price) / entry_price

    # 1. 손절
    if config.stop_loss_pct > 0 and roi <= -config.stop_loss_pct:
        return True, f"STOP_LOSS roi={roi*100:.1f}% ≤ -{config.stop_loss_pct*100:.0f}%"

    # 2. 익절
    if config.take_profit_pct > 0 and roi >= config.take_profit_pct:
        return True, f"TAKE_PROFIT roi={roi*100:.1f}% ≥ +{config.take_profit_pct*100:.0f}%"

    # 3. 트레일링 스탑
    if config.trailing_stop_pct > 0 and high_price > 0:
        if is_long:
            trail_roi = (current_price - high_price) / high_price
        else:
            trail_roi = (high_price - current_price) / high_price  # high_price = 저점
        if trail_roi <= -config.trailing_stop_pct:
            return True, f"TRAILING_STOP drawdown={trail_roi*100:.1f}% ≤ -{config.trailing_stop_pct*100:.0f}%"

    return False, ""


def strategy_summary() -> str:
    """현재 4가지 전략 요약 텍스트"""
    lines = ["=" * 60, "  Copy Perp 전략 프리셋", "=" * 60]
    for sid, cfg in STRATEGY_PRESETS.items():
        traders = select_traders_for_strategy(cfg)
        trader_names = ", ".join(t["trader_alias"] for t in traders) or "없음 (데이터 부족)"
        sl_str = f"{cfg.stop_loss_pct*100:.0f}%" if cfg.stop_loss_pct > 0 else "없음"
        tp_str = f"{cfg.take_profit_pct*100:.0f}%" if cfg.take_profit_pct > 0 else "없음"
        tr_str = f"고점-{cfg.trailing_stop_pct*100:.0f}%" if cfg.trailing_stop_pct > 0 else "없음"
        kelly_str = f"켈리 {cfg.kelly_fraction*100:.0f}%" if cfg.use_kelly else f"고정 {cfg.copy_ratio*100:.0f}%"
        lines += [
            f"\n  [{sid.upper()}] {cfg.label}",
            f"  {cfg.description}",
            f"  복사비율: {kelly_str}  |  최대포지션: ${cfg.max_position_usdc:.0f}",
            f"  손절: {sl_str}  |  익절: {tp_str}  |  트레일링: {tr_str}",
            f"  선택 트레이더: {trader_names}",
        ]
    lines.append("=" * 60)
    return "\n".join(lines)


def _calc_stop_from_preset(
    entry_price: float,
    side: str,
    preset: dict,
) -> tuple[Optional[float], Optional[float]]:
    """
    strategy_presets dict → (stop_loss_price, take_profit_price) 계산
    preset은 PRESETS[key] dict (stop_loss_pct, take_profit_pct 키 포함)
    """
    sl_pct = float(preset.get("stop_loss_pct", 0) or 0)
    tp_pct = float(preset.get("take_profit_pct", 0) or 0)

    # StrategyConfig 호환 임시 객체 생성
    class _P:
        stop_loss_pct    = sl_pct
        take_profit_pct  = tp_pct
        trailing_stop_pct = float(preset.get("trailing_stop_pct", 0) or 0)

    return calc_stop_price(entry_price, side, _P())


def _should_stop_from_preset(
    entry_price: float,
    current_price: float,
    high_price: float,
    side: str,
    preset: dict,
) -> tuple[bool, str]:
    """strategy_presets dict → should_stop 호환"""
    class _P:
        stop_loss_pct    = float(preset.get("stop_loss_pct", 0) or 0)
        take_profit_pct  = float(preset.get("take_profit_pct", 0) or 0)
        trailing_stop_pct = float(preset.get("trailing_stop_pct", 0) or 0)

    return should_stop(entry_price, current_price, high_price, side, _P())


if __name__ == "__main__":
    print(strategy_summary())
