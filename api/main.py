"""
Copy Perp FastAPI 백엔드
"""
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
import asyncio
import sys, os, warnings
warnings.filterwarnings("ignore")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from db.models import DB
from pacifica.client import PacificaClient, PriceStream, PositionPoller
from core.copy_engine import CopyEngine

app = FastAPI(title="Copy Perp API", version="0.1.0")
db = DB()
price_stream = PriceStream()

CEO_WALLET = os.getenv("CEO_WALLET_ADDRESS", "3AHZqrocSguMuo9sUUP8G8YN8NwHwWV2DPUQvbDvtfaQ")
AGENT_PK   = os.getenv("AGENT_PRIVATE_KEY", "")
AGENT_PUB  = os.getenv("AGENT_PUBLIC_KEY", "")


# ─── 시작/종료 ───────────────────────────────────────

@app.on_event("startup")
async def startup():
    await db.init()
    # 가격 스트림 백그라운드 시작
    asyncio.create_task(price_stream.start())

@app.on_event("shutdown")
async def shutdown():
    price_stream.stop()


# ─── 상태 조회 ───────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok", "service": "Copy Perp"}

@app.get("/health")
def health():
    btc = price_stream.latest.get("BTC", {})
    return {
        "status": "ok",
        "ws_connected": bool(btc),
        "btc_price": btc.get("mark"),
        "btc_funding": btc.get("funding"),
        "btc_oi": btc.get("open_interest"),
    }

@app.get("/prices")
def get_prices(symbol: Optional[str] = None):
    if symbol:
        data = price_stream.latest.get(symbol.upper())
        if not data:
            raise HTTPException(404, f"{symbol} 데이터 없음")
        return data
    return price_stream.latest

@app.get("/wallet/{address}")
def get_wallet(address: str):
    client = PacificaClient(account_address=address)
    balance = client.get_balance()
    positions = client.get_positions()
    return {
        "address": address,
        "balance_usd": balance,
        "positions": positions,
    }


# ─── 트레이더 등록 ───────────────────────────────────

class TraderIn(BaseModel):
    address: str
    alias: Optional[str] = ""

@app.post("/traders")
async def register_trader(body: TraderIn):
    await db.add_trader(body.address, body.alias)
    return {"ok": True, "trader": body.address}

@app.get("/traders/{address}")
def get_trader(address: str):
    client = PacificaClient(account_address=address)
    return {
        "address": address,
        "balance_usd": client.get_balance(),
        "positions": client.get_positions(),
    }


# ─── 팔로워 등록 ─────────────────────────────────────

class FollowerIn(BaseModel):
    follower_address: str
    trader_address: str
    agent_private_key: str          # 팔로워의 Agent Key (서버 보관)
    copy_ratio: float = 0.5         # 잔고의 50%
    max_position_usd: float = 100   # 최대 $100

@app.post("/followers")
async def register_follower(body: FollowerIn):
    # 트레이더 자동 등록
    await db.add_trader(body.trader_address)
    # 팔로워 등록
    await db.add_follower(
        follower_id=body.follower_address,
        trader_id=body.trader_address,
        private_key=body.agent_private_key,
        copy_ratio=body.copy_ratio,
        max_position_usd=body.max_position_usd,
    )
    return {"ok": True, "follower": body.follower_address, "trader": body.trader_address}


# ─── 복사 거래 조회 ──────────────────────────────────

@app.get("/trades")
async def get_trades(limit: int = 50):
    trades = await db.get_copy_trades(limit)
    return {"trades": trades, "count": len(trades)}


# ─── 리더보드 ────────────────────────────────────────

@app.get("/leaderboard")
def get_leaderboard():
    client = PacificaClient(account_address=CEO_WALLET)
    try:
        data = client.get_leaderboard()
        return data
    except Exception as e:
        raise HTTPException(500, str(e))
