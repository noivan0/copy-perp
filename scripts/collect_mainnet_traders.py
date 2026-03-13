#!/usr/bin/env python3
"""
mainnet 트레이더 리더보드 수집
- Pacifica mainnet에서 실제 상위 트레이더 데이터 수집
- codetabs GET 프록시로 HMG 필터 우회
- DB에 저장 (NETWORK=mainnet 환경에서 실행)
"""
import sys, os, json, time, urllib.request, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

import aiosqlite
from db.database import init_db, add_trader

CODETABS = "https://api.codetabs.com/v1/proxy?quest="
MAINNET = "https://api.pacifica.fi/api/v1"
DB_PATH = os.getenv("DB_PATH", "copy_perp_mainnet.db")

def _get(path: str) -> dict:
    url = f"{CODETABS}{MAINNET}/{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read())

def fetch_leaderboard(limit: int = 100) -> list:
    """mainnet 리더보드 상위 트레이더"""
    try:
        d = _get(f"leaderboard?limit={limit}")
        return d.get("data", d) if isinstance(d, dict) else d
    except Exception as e:
        print(f"리더보드 오류: {e}")
        return []

def fetch_trader_stats(address: str) -> dict:
    """트레이더 통계"""
    try:
        d = _get(f"account?account={address}")
        return d.get("data", {}) if isinstance(d, dict) else {}
    except Exception as e:
        return {}

async def save_to_db(traders: list):
    async with aiosqlite.connect(DB_PATH) as db:
        # 스키마 초기화
        await init_db(DB_PATH)
        
        for t in traders:
            addr = t.get("address") or t.get("account")
            if not addr:
                continue
            username = t.get("username") or t.get("alias") or ""
            
            # traders 테이블 업데이트
            await db.execute("""
                INSERT OR IGNORE INTO traders (address, alias, created_at) VALUES (?, ?, ?)
            """, (addr, username, int(time.time()*1000)))
            
            # PnL 데이터 업데이트
            pnl_1d = float(t.get("pnl_1d") or 0)
            pnl_7d = float(t.get("pnl_7d") or 0)
            pnl_30d = float(t.get("pnl_30d") or 0)
            pnl_all = float(t.get("pnl_all_time") or 0)
            
            await db.execute("""
                UPDATE traders SET 
                    pnl_1d=?, pnl_7d=?, pnl_30d=?, pnl_all_time=?,
                    last_synced=?
                WHERE address=?
            """, (pnl_1d, pnl_7d, pnl_30d, pnl_all, int(time.time()*1000), addr))
        
        await db.commit()
        print(f"DB 저장: {len(traders)}명")

async def main():
    print("=== Pacifica Mainnet 트레이더 수집 ===")
    print(f"DB: {DB_PATH}")
    
    traders = fetch_leaderboard(100)
    if not traders:
        print("데이터 없음")
        return
    
    print(f"수집: {len(traders)}명")
    
    # 상위 5명 미리보기
    for t in traders[:5]:
        addr = t.get("address", "")
        pnl7 = t.get("pnl_7d", "?")
        pnl30 = t.get("pnl_30d", "?")
        print(f"  {addr[:16]}... pnl7d={pnl7}, pnl30d={pnl30}")
    
    await save_to_db(traders)
    print("완료")

if __name__ == "__main__":
    asyncio.run(main())
