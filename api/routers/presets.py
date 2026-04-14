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

    # ── CRS 실시간 계산 기반 traders 재계산 ──────────────────────────────────
    # DB traders.grade 컬럼이 없으므로 DB rows → CRS 계산 → grade 기반 분류
    try:
        from api.routers.ranked import _fetch_rows_from_db, _leaderboard_row_to_crs
        from core.strategy_presets import PRESETS

        _rows = await _fetch_rows_from_db(200)
        grade_order = {"S": 4, "A": 3, "B": 2, "C": 1}
        live_by_grade: dict = {}
        for row in _rows:
            try:
                crs_data = _leaderboard_row_to_crs(row)
                g = crs_data.get("grade", "D")
                if g in grade_order and not crs_data.get("disqualified", False):
                    live_by_grade.setdefault(g, []).append(crs_data.get("address"))
            except Exception:
                pass

        # grade 우선순위 (S → A → B → C 순으로 fallback)
        _grade_fallback = ["S", "A", "B", "C"]
        for preset in presets:
            grade_filter = PRESETS.get(preset.get("key",""), {}).get("grade_filter", ["S","A"])
            n = preset.get("n_traders", 2)
            live_addrs = []
            for g in grade_filter:
                live_addrs.extend(live_by_grade.get(g, []))
            # 지정 grade 부족 시 하위 grade로 보완 (S 없으면 A, A 없으면 B)
            if len(live_addrs) < n:
                for fallback_g in _grade_fallback:
                    if fallback_g not in grade_filter:
                        for addr in live_by_grade.get(fallback_g, []):
                            if addr not in live_addrs:
                                live_addrs.append(addr)
                            if len(live_addrs) >= n:
                                break
                    if len(live_addrs) >= n:
                        break
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

    # Live ranked 기반 traders 재계산 (CRS grade 사용 — traders 테이블엔 grade 컬럼 없음)
    try:
        from api.routers.ranked import _leaderboard_row_to_crs, _ranked_cache
        from api.main import get_db as _get_db
        _db = await _get_db()
        if _db:
            from db.database import get_leaderboard as _get_lb
            grade_filter = preset.get("grade_filter", ["S", "A"])
            n = preset.get("n_traders", 2)
            _grade_order = {"S": 4, "A": 3, "B": 2, "C": 1, "D": 0}
            leaders = await _get_lb(_db, 200)
            ranked = [_leaderboard_row_to_crs(dict(r)) for r in leaders]
            # grade_filter 우선, 부족 시 S→A→B→C fallback
            _by_grade: dict = {}
            for t in ranked:
                g = t.get("grade", "D")
                if not t.get("disqualified", False):
                    _by_grade.setdefault(g, []).append(t["address"])
            live_addrs: list = []
            for g in grade_filter:
                live_addrs.extend(_by_grade.get(g, []))
            if len(live_addrs) < n:
                for fb_g in ["S", "A", "B", "C"]:
                    if fb_g not in grade_filter:
                        for addr in _by_grade.get(fb_g, []):
                            if addr not in live_addrs:
                                live_addrs.append(addr)
                            if len(live_addrs) >= n:
                                break
                    if len(live_addrs) >= n:
                        break
            if live_addrs:
                traders = live_addrs[:n]
    except Exception as _e:
        logger.warning(f"preset/{name} live traders 재계산 실패 (FALLBACK 유지): {_e}")

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

    # CRS 실시간 계산 기반 traders 재계산 (DB grade 컬럼 없음, grade fallback 포함)
    try:
        from api.routers.ranked import _fetch_rows_from_db, _leaderboard_row_to_crs
        grade_filter = preset.get("grade_filter", ["S","A"])
        n_traders = preset.get("n_traders", 2)
        _rows = await _fetch_rows_from_db(200)
        # CRS 계산 후 grade별 분류
        _by_grade: dict = {}
        for row in _rows:
            try:
                crs_data = _leaderboard_row_to_crs(row)
                g = crs_data.get("grade", "D")
                if g not in ("D",) and not crs_data.get("disqualified", False):
                    _by_grade.setdefault(g, []).append(crs_data.get("address"))
            except Exception:
                pass
        # grade_filter 우선, 부족 시 S→A→B→C 순 fallback
        live_addrs = []
        for g in grade_filter:
            live_addrs.extend(_by_grade.get(g, []))
        if len(live_addrs) < n_traders:
            for fb_g in ["S","A","B","C"]:
                if fb_g not in grade_filter:
                    for addr in _by_grade.get(fb_g, []):
                        if addr not in live_addrs:
                            live_addrs.append(addr)
                        if len(live_addrs) >= n_traders:
                            break
                if len(live_addrs) >= n_traders:
                    break
        if live_addrs:
            traders = live_addrs[:n_traders]
    except Exception as _e:
        logger.warning(f"apply/{name} live traders 재계산 실패 (FALLBACK 유지): {_e}")

    if not traders:
        raise HTTPException(status_code=503, detail={"error": "Trader selection failed. Please try again."})

    # followers/onboard 직접 호출
    # SL/TP: strategy_presets.py는 소수형(0.08=8%) 저장, onboard validator는 퍼센트형(0.1~99) 기대
    # 소수형 → 퍼센트형 변환 (0.08 → 8.0)
    _sl_raw = preset.get("stop_loss_pct")
    _tp_raw = preset.get("take_profit_pct")
    _sl_pct = round(_sl_raw * 100, 2) if _sl_raw is not None and _sl_raw <= 1.0 else _sl_raw
    _tp_pct = round(_tp_raw * 100, 2) if _tp_raw is not None and _tp_raw <= 1.0 else _tp_raw

    from api.routers.followers import OnboardRequest, onboard_follower
    onboard_body = OnboardRequest(
        follower_address=body.follower_address,
        copy_ratio=preset["copy_ratio"],
        max_position_usdc=preset["max_position_usdc"],
        traders=traders,
        stop_loss_pct=_sl_pct,
        take_profit_pct=_tp_pct,
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
        logger.error(f"preset onboard 오류: {e}", exc_info=True); raise HTTPException(status_code=500, detail={"error": "Onboarding failed — please retry", "code": "ONBOARD_ERROR"})

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
