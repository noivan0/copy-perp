"""
scripts/register_traders.py
백테스팅 결과 기반 Tier1 트레이더 자동 등록 스크립트

실행: python3 scripts/register_traders.py
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

from db.database import init_db, add_trader


BACKTEST_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "backtest_results.json")


async def register_traders(db_path: str = "copy_perp.db", dry_run: bool = False):
    """백테스팅 결과에서 Tier1/Tier2 트레이더를 DB에 등록"""
    if not os.path.exists(BACKTEST_FILE):
        print(f"❌ {BACKTEST_FILE} 없음 — 먼저 백테스팅 실행 필요")
        return []

    with open(BACKTEST_FILE) as f:
        data = json.load(f)

    tier1 = data.get("tier1", [])
    tier2 = data.get("tier2", [])
    all_traders = tier1 + tier2

    print(f"등록 대상: Tier1={len(tier1)}명, Tier2={len(tier2)}명, 합계={len(all_traders)}명")

    if dry_run:
        print("[DRY RUN] 실제 DB 변경 없음")
        for t in all_traders:
            tier = "Tier1" if t in tier1 else "Tier2"
            print(f"  [{tier}] {t['address'][:16]}... alias={t['alias']} roi={t.get('roi_raw', 0):.1f}%")
        return all_traders

    db = await init_db(db_path)
    registered = []
    skipped = []

    for t in all_traders:
        addr = t["address"]
        alias = t.get("alias") or addr[:8]
        tier = "Tier1" if t in tier1 else "Tier2"

        try:
            # 이미 존재하면 active=1 설정
            existing = await db.execute_fetchone(
                "SELECT address FROM traders WHERE address=?", (addr,)
            ) if hasattr(db, 'execute_fetchone') else None

            # aiosqlite 방식
            async with db.execute("SELECT address FROM traders WHERE address=?", (addr,)) as cur:
                existing = await cur.fetchone()

            if existing:
                await db.execute("UPDATE traders SET active=1 WHERE address=?", (addr,))
                skipped.append(addr)
                print(f"  [UPDATE] {tier} {alias} ({addr[:12]}...) — 이미 존재, active=1")
            else:
                await add_trader(db, addr, alias)
                registered.append(addr)
                print(f"  [NEW]    {tier} {alias} ({addr[:12]}...) roi={t.get('roi_raw', 0):.1f}%")

        except Exception as e:
            print(f"  [ERROR]  {addr[:12]}... — {e}")

    await db.commit()
    await db.close()

    print()
    print(f"✅ 등록 완료: 신규={len(registered)}명, 업데이트={len(skipped)}명")
    return registered + skipped


async def verify_registration(db_path: str = "copy_perp.db"):
    """등록된 트레이더 수 검증"""
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT COUNT(*) as c FROM traders WHERE active=1"
        ) as cur:
            row = await cur.fetchone()
        count = row["c"]

        async with db.execute(
            "SELECT address, alias, pnl_all_time FROM traders WHERE active=1 ORDER BY pnl_all_time DESC LIMIT 5"
        ) as cur:
            top5 = await cur.fetchall()

    print(f"DB 활성 트레이더: {count}명")
    print("TOP 5:")
    for r in top5:
        print(f"  {r['address'][:16]}... {r['alias']} pnl={float(r['pnl_all_time']):,.0f}")
    return count


async def main():
    print("=" * 60)
    print("트레이더 등록 스크립트 v1.0")
    print("=" * 60)

    # 1. 등록
    registered = await register_traders(dry_run=False)

    print()
    print("=" * 60)
    print("등록 검증")
    print("=" * 60)
    count = await verify_registration()

    return count


if __name__ == "__main__":
    count = asyncio.run(main())
    print(f"\n최종: {count}명 활성 트레이더 등록됨")
