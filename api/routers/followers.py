"""
팔로워 라우터
POST /followers                     — 팔로워 등록 (레퍼럴 추적 포함)
GET  /followers/{address}           — 팔로워 현황
PUT  /followers/{address}/settings  — 복사 설정 변경 (비율, 최대금액)
DELETE /followers/{address}         — 팔로우 해지
GET  /followers/{address}/referral  — 내 레퍼럴 링크 + 포인트
"""

from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
from typing import Optional
import time

from db.database import add_follower, get_followers
from fuul.referral import fuul

router = APIRouter(prefix="/followers", tags=["followers"])


class FollowerRegister(BaseModel):
    address: str
    trader_address: str
    copy_ratio: float = Field(default=1.0, ge=0.1, le=5.0,
                              description="복사 비율 (0.1~5.0배)")
    max_position_usdc: float = Field(default=100.0, ge=10.0, le=10000.0,
                                     description="포지션당 최대 USDC")
    referrer: Optional[str] = None


class FollowerSettings(BaseModel):
    copy_ratio: Optional[float] = Field(None, ge=0.1, le=5.0)
    max_position_usdc: Optional[float] = Field(None, ge=10.0, le=10000.0)


@router.post("")
async def register_follower(body: FollowerRegister, background_tasks: BackgroundTasks):
    """팔로워 등록 + 레퍼럴 추적"""
    from api.main import db, monitors, engine
    from core.position_monitor import PositionMonitor

    await add_follower(
        db,
        body.address,
        body.trader_address,
        body.copy_ratio,
        body.max_position_usdc,
    )

    # 레퍼럴 추적
    if body.referrer:
        background_tasks.add_task(
            fuul.track_referral, body.referrer, body.address
        )

    # 포지션 모니터 시작
    if body.trader_address not in monitors:
        monitor = PositionMonitor(body.trader_address, engine.on_fill)
        monitors[body.trader_address] = monitor
        background_tasks.add_task(monitor.start)

    referral_link = fuul.generate_referral_link(body.address)

    return {
        "ok": True,
        "follower": body.address,
        "trader": body.trader_address,
        "copy_ratio": body.copy_ratio,
        "max_position_usdc": body.max_position_usdc,
        "referral_link": referral_link,
        "note": "Builder Code 승인은 프론트엔드에서 서명 처리 필요",
    }


@router.get("/{address}")
async def get_follower(address: str):
    from api.main import db
    async with db.execute(
        """SELECT f.*, t.alias as trader_alias
           FROM followers f
           LEFT JOIN traders t ON f.trader_address = t.address
           WHERE f.address = ?""",
        (address,)
    ) as cur:
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "팔로워를 찾을 수 없습니다")

    data = dict(row)
    data["points"] = fuul.get_points(address)
    data["referral_link"] = fuul.generate_referral_link(address)
    return {"data": data}


@router.put("/{address}/settings")
async def update_settings(address: str, body: FollowerSettings):
    """복사 설정 변경"""
    from api.main import db
    updates = []
    params = []
    if body.copy_ratio is not None:
        updates.append("copy_ratio = ?")
        params.append(body.copy_ratio)
    if body.max_position_usdc is not None:
        updates.append("max_position_usdc = ?")
        params.append(body.max_position_usdc)

    if not updates:
        raise HTTPException(400, "변경할 항목이 없습니다")

    params.append(address)
    await db.execute(
        f"UPDATE followers SET {', '.join(updates)} WHERE address = ?",
        params
    )
    await db.commit()
    return {"ok": True, "address": address}


@router.delete("/{address}")
async def unfollow(address: str):
    """팔로우 해지"""
    from api.main import db
    await db.execute(
        "UPDATE followers SET active = 0 WHERE address = ?", (address,)
    )
    await db.commit()
    return {"ok": True, "unfollowed": address}


@router.get("/{address}/referral")
async def get_referral(address: str):
    """레퍼럴 링크 + 포인트 현황"""
    return {
        "address": address,
        "referral_link": fuul.generate_referral_link(address),
        "points": fuul.get_points(address),
        "leaderboard_rank": None,  # 추후 구현
    }
