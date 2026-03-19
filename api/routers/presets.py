"""
api/routers/presets.py — 시나리오 프리셋 API

GET  /presets                       — 4개 프리셋 목록 + 시뮬 PnL
GET  /presets/{name}                — 특정 프리셋 상세
GET  /presets/{name}/sim            — 자본 N 기준 예상 PnL
POST /presets/{name}/apply          — 프리셋 적용 (팔로워 온보딩)
"""
import logging
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, BackgroundTasks, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/presets", tags=["presets"])

_ROOT   = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DB_PATH = os.path.join(_ROOT, "mainnet_tracker.db")


# ── GET /presets ──────────────────────────────────────────────────
@router.get("")
async def list_presets(capital: float = Query(default=1000.0, ge=1.0, le=100000.0)) -> dict:
    """4개 프리셋 목록 + 시뮬 PnL"""
    from core.strategy_presets import list_presets_with_sim
    presets = list_presets_with_sim(capital=capital, db_path=_DB_PATH)
    return {"presets": presets, "capital_used": capital}


# ── GET /presets/{name} ───────────────────────────────────────────
@router.get("/{name}")
async def get_preset_detail(
    name: str,
    capital: float = Query(default=1000.0, ge=1.0, le=100000.0),
) -> dict:
    """특정 프리셋 상세 (트레이더 목록 + 시뮬 PnL 포함)"""
    from core.strategy_presets import PRESETS, get_preset_sim_pnl, resolve_traders
    if name not in PRESETS:
        raise HTTPException(status_code=404, detail={"error": f"프리셋 '{name}' 없음. 유효한 값: {list(PRESETS.keys())}"})
    preset  = PRESETS[name]
    sim     = get_preset_sim_pnl(name, capital=capital, db_path=_DB_PATH)
    traders = resolve_traders(name, db_path=_DB_PATH)
    return {**preset, "traders": traders, "sim_pnl": {**sim, "capital": capital}}


# ── GET /presets/{name}/sim ───────────────────────────────────────
@router.get("/{name}/sim")
async def sim_preset_pnl(
    name: str,
    capital: float = Query(default=1000.0, ge=1.0, le=100000.0),
) -> dict:
    """자본 기준 예상 PnL 계산"""
    from core.strategy_presets import PRESETS, get_preset_sim_pnl
    if name not in PRESETS:
        raise HTTPException(status_code=404, detail={"error": f"프리셋 '{name}' 없음"})
    sim = get_preset_sim_pnl(name, capital=capital, db_path=_DB_PATH)
    preset = PRESETS[name]
    return {
        "preset":       name,
        "label":        preset["label"],
        "capital":      capital,
        "copy_ratio":   preset["copy_ratio"],
        "max_position_usdc": preset["max_position_usdc"],
        **sim,
    }


# ── POST /presets/{name}/apply ────────────────────────────────────
class ApplyPresetRequest(BaseModel):
    follower_address: str
    capital_usdc: Optional[float] = None   # 참고용 (실제 온체인 자산과 별개)

@router.post("/{name}/apply")
async def apply_preset(
    name: str,
    body: ApplyPresetRequest,
    background_tasks: BackgroundTasks,
    request: Request,
) -> dict:
    """
    프리셋 적용 → followers/onboard 내부 호출
    copy_ratio, max_position_usdc, traders 자동 설정
    """
    from core.strategy_presets import PRESETS, resolve_traders, get_preset_sim_pnl
    if name not in PRESETS:
        raise HTTPException(status_code=404, detail={"error": f"프리셋 '{name}' 없음"})

    preset  = PRESETS[name]
    traders = resolve_traders(name, db_path=_DB_PATH)

    if not traders:
        raise HTTPException(status_code=503, detail={"error": "트레이더 선별 실패. 잠시 후 재시도하세요."})

    # followers/onboard 직접 호출
    from api.routers.followers import OnboardRequest, onboard_follower
    onboard_body = OnboardRequest(
        follower_address=body.follower_address,
        copy_ratio=preset["copy_ratio"],
        max_position_usdc=preset["max_position_usdc"],
        traders=traders,
    )

    try:
        result = await onboard_follower(
            request=request,
            body=onboard_body,
            background_tasks=background_tasks,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": f"온보딩 실패: {str(e)}"})

    # 시뮬 PnL 첨부
    capital = body.capital_usdc or 1000.0
    sim     = get_preset_sim_pnl(name, capital=capital, db_path=_DB_PATH)

    return {
        **result,
        "preset":        name,
        "preset_label":  preset["label"],
        "copy_ratio":    preset["copy_ratio"],
        "max_position_usdc": preset["max_position_usdc"],
        "traders_assigned":  traders,
        "sim_pnl": {
            "capital":       capital,
            "pnl_30d":       sim["pnl_30d"],
            "roi_30d_pct":   sim["roi_30d_pct"],
            "data_source":   sim.get("data_source", "estimated"),
        },
    }
