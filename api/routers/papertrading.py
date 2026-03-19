"""
api/routers/papertrading.py
4개 전략 페이퍼트레이딩 현황 API

GET /papertrading/status          — 4개 전략 비교 요약
GET /papertrading/{strategy}      — 특정 전략 상세 (거래 이력 포함)
GET /papertrading/{strategy}/equity-curve — equity curve
"""
import json
import logging
import os

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/papertrading", tags=["papertrading"])

_BASE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "papertrading", "results"
)
STRATEGY_NAMES = ["safe", "default", "balanced", "aggressive"]
STRATEGY_LABELS = {
    "safe":       "🛡 안전형",
    "default":    "⚙️ 기본형",
    "balanced":   "⚖️ 균형형",
    "aggressive": "⚡ 공격형",
}


def _load_portfolio(name: str) -> dict:
    path = os.path.join(_BASE, f"strategy_{name}", "portfolio.json")
    if not os.path.exists(path):
        return {
            "strategy": name, "label": STRATEGY_LABELS.get(name, name),
            "capital": 10000.0, "total_pnl": 0, "net_pnl": 0,
            "roi_pct": 0, "total_trades": 0, "win_rate": 0,
            "profit_factor": 0, "max_dd_pct": 0,
            "session_count": 0, "status": "no_data",
            "started_at": None, "updated_at": None,
        }
    with open(path) as f:
        data = json.load(f)
    data["status"] = "active"
    return data


def _load_comparison() -> dict:
    path = os.path.join(_BASE, "comparison.json")
    if not os.path.exists(path):
        return {"strategies": [], "note": "아직 데이터 없음"}
    with open(path) as f:
        return json.load(f)


@router.get("/status")
async def papertrading_status():
    """4개 전략 실시간 비교 요약"""
    comparison = _load_comparison()
    if not comparison.get("strategies"):
        # comparison.json 없으면 직접 조합
        summaries = []
        for name in STRATEGY_NAMES:
            p = _load_portfolio(name)
            from papertrading.multi_strategy_engine import STRATEGIES
            summaries.append({
                "strategy":             name,
                "label":                STRATEGY_LABELS.get(name, name),
                "capital":              p.get("capital", 10000),
                "total_pnl":            p.get("total_pnl", 0),
                "net_pnl":              p.get("net_pnl", 0),
                "roi_pct":              p.get("roi_pct", 0),
                "total_trades":         p.get("total_trades", 0),
                "win_rate":             p.get("win_rate", 0),
                "profit_factor":        p.get("profit_factor", 0),
                "max_dd_pct":           p.get("max_dd_pct", 0),
                "session_count":        p.get("session_count", 0),
                "expected_monthly_roi": STRATEGIES[name]["expected_monthly_roi"],
                "status":               p.get("status", "no_data"),
                "updated_at":           p.get("updated_at"),
            })
        return {
            "strategies":     summaries,
            "initial_capital": 10000,
            "note":           "mainnet 실데이터 페이퍼트레이딩",
            "status":         "initializing",
        }

    return comparison


@router.get("/{strategy}")
async def papertrading_detail(strategy: str):
    """특정 전략 상세 — 거래 이력 + 통계 포함"""
    if strategy not in STRATEGY_NAMES:
        raise HTTPException(
            status_code=404,
            detail=f"전략 '{strategy}'를 찾을 수 없습니다. 유효값: {STRATEGY_NAMES}"
        )

    p = _load_portfolio(strategy)

    # 최근 거래 이력 (trade_log에서 추출)
    trades = p.pop("trade_log", [])[-50:]  # 최근 50건
    equity = p.pop("equity_series", [])[-100:]  # 최근 100점

    # 트레이더별 기여 집계
    by_trader: dict = {}
    for t in trades:
        alias = t.get("trader", "?")
        if alias not in by_trader:
            by_trader[alias] = {"trades": 0, "net_pnl": 0.0, "wins": 0}
        by_trader[alias]["trades"] += 1
        by_trader[alias]["net_pnl"] = round(
            by_trader[alias]["net_pnl"] + t.get("net", 0), 4
        )
        if t.get("copy_pnl", 0) > 0:
            by_trader[alias]["wins"] += 1

    return {
        **p,
        "recent_trades": trades,
        "equity_points":  len(equity),
        "by_trader":      by_trader,
    }


@router.get("/{strategy}/equity-curve")
async def papertrading_equity_curve(strategy: str):
    """Equity curve 조회 (차트용)"""
    if strategy not in STRATEGY_NAMES:
        raise HTTPException(status_code=404, detail=f"전략 없음: {strategy}")

    path = os.path.join(_BASE, f"strategy_{strategy}", "portfolio.json")
    if not os.path.exists(path):
        return {"strategy": strategy, "data": [], "status": "no_data"}

    with open(path) as f:
        data = json.load(f)

    equity_series = data.get("equity_series", [])
    return {
        "strategy": strategy,
        "label":    STRATEGY_LABELS.get(strategy, strategy),
        "data":     equity_series,
        "points":   len(equity_series),
        "current":  equity_series[-1] if equity_series else 10000,
        "initial":  10000,
        "roi_pct":  round((equity_series[-1] - 10000) / 10000 * 100, 4) if equity_series else 0,
    }
