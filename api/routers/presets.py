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

    # ── Ranked API 기반 traders 재계산 (DB grade 컬럼 없음 → CRS 실시간 계산 결과 사용) ──
    # resolve_traders()가 로컬 mainnet_tracker.db를 참조해 FALLBACK 주소를 반환하는 문제 해결.
    # DB traders 테이블에 grade 컬럼이 없으므로 /traders/ranked에서 CRS 계산된 결과를 사용.
    try:
        from api.routers.ranked import get_ranked_traders
        from core.strategy_presets import PRESETS
        from fastapi import Request as _Request
        import starlette.requests as _sr

        # ranked API 호출 (min_grade=C, exclude_disqualified=True, limit=100)
        _ranked_result = await get_ranked_traders(
            request=None,  # request=None 처리됨 (rate limit IP 없으면 스킵)
            limit=100,
            min_grade="C",
            exclude_disqualified=True,
        )
        _ranked_data = _ranked_result.get("data", []) if isinstance(_ranked_result, dict) else []

        # grade별 주소 캐시 구성
        grade_order = {"S": 4, "A": 3, "B": 2, "C": 1}
        live_by_grade: dict = {}
        for t in _ranked_data:
            g = t.get("grade", "D")
            if g in grade_order:
                live_by_grade.setdefault(g, []).append(t.get("address"))

        # 각 프리셋의 traders 필드를 ranked 결과로 교체
        for preset in presets:
            grade_filter = PRESETS.get(preset.get("key",""), {}).get("grade_filter", ["S","A"])
            n = preset.get("n_traders", 2)
            live_addrs = []
            for g in grade_filter:
                live_addrs.extend(live_by_grade.get(g, []))
            if live_addrs:
                preset["traders"] = live_addrs[:n]
    except Exception as _e:
        logger.warning(f"presets live traders 재계산 실패 (FALLBACK 유지): {_e}")

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
        raise HTTPException(status_code=404, detail={"error": f"Preset '{name}' not found. Valid: {list(PRESETS.keys())}"})
    preset  = PRESETS[name]
    sim     = get_preset_sim_pnl(name, capital=capital, db_path=_DB_PATH)
    traders = resolve_traders(name, db_path=_DB_PATH)

    # Live DB 기반 traders 재계산 (FALLBACK 주소 대신 실제 DB 트레이더 사용)
    try:
        from api.deps import _get_db_direct
        _db = _get_db_direct()
        if _db:
            grade_filter = preset.get("grade_filter", ["S","A"])
            n = preset.get("n_traders", 2)
            placeholders = ",".join("?" * len(grade_filter))
            async with _db.execute(
                f"SELECT address FROM traders WHERE active=1 AND grade IN ({placeholders}) ORDER BY pnl_30d DESC NULLS LAST LIMIT ?",
                (*grade_filter, n),
            ) as cur:
                rows = await cur.fetchall()
            live_addrs = [row["address"] for row in rows]
            if live_addrs:
                traders = live_addrs
    except Exception as _e:
        logger.warning(f"preset/{name} live traders 재계산 실패: {_e}")

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
        raise HTTPException(status_code=404, detail={"error": f"Preset '{name}' not found"})
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
        raise HTTPException(status_code=404, detail={"error": f"Preset '{name}' not found"})

    preset  = PRESETS[name]
    traders = resolve_traders(name, db_path=_DB_PATH)

    # Live DB 기반 traders 재계산 (FALLBACK 주소 대신 실제 DB 트레이더 사용)
    try:
        from api.deps import _get_db_direct
        _db_apply = _get_db_direct()
        if _db_apply:
            grade_filter = preset.get("grade_filter", ["S","A"])
            n_traders = preset.get("n_traders", 2)
            placeholders = ",".join("?" * len(grade_filter))
            async with _db_apply.execute(
                f"SELECT address FROM traders WHERE active=1 AND grade IN ({placeholders}) ORDER BY pnl_30d DESC NULLS LAST LIMIT ?",
                (*grade_filter, n_traders),
            ) as cur:
                rows = await cur.fetchall()
            live_addrs = [row["address"] for row in rows]
            if live_addrs:
                traders = live_addrs
    except Exception as _e:
        logger.warning(f"apply/{name} live traders 재계산 실패 (FALLBACK 유지): {_e}")

    if not traders:
        raise HTTPException(status_code=503, detail={"error": "Trader selection failed. Please try again."})

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
            # 명시적으로 auth 헤더 None 전달 (직접 호출 시 Header() 파싱 미작동 방지)
            x_privy_token=None,
            authorization=None,
        )
    except HTTPException as e:
        # HTTPException(401/429 등)은 그대로 re-raise (500 래핑 금지)
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": f"Onboarding failed: {str(e)}"})

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
