#!/usr/bin/env python3
"""
Mainnet 트레이더 선별 스크립트
- leaderboard 100명 수집
- 필터: pnl_30d > $10k AND roi_30d > 5% AND pnl_7d > 0
- 결과를 mainnet_selected_traders.json 저장
- DB에 자동 등록

Usage:
    python3 scripts/select_mainnet_traders.py
    python3 scripts/select_mainnet_traders.py --limit 25000 --output /tmp/traders.json
"""
import sys
import os
import json
import time
import asyncio
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

# DB
import aiosqlite
from db.database import init_db

# 출력 경로 (프로젝트 루트)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUTPUT = os.path.join(PROJECT_ROOT, "mainnet_selected_traders.json")


def fetch_leaderboard(limit: int = 100) -> list:
    """codetabs 프록시로 Mainnet 리더보드 수집"""
    import urllib.request
    import urllib.parse

    target = f"https://api.pacifica.fi/api/v1/leaderboard?limit={limit}"
    url = "https://api.codetabs.com/v1/proxy/?quest=" + urllib.parse.quote(target, safe=":/?=&")
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "CopyPerp/1.0",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read())

    # codetabs는 raw JSON 그대로 반환
    if isinstance(data, dict):
        return data.get("data", [])
    return data or []


def filter_traders(traders: list) -> list:
    """
    선별 기준:
    - pnl_30d > $10,000
    - roi_30d = pnl_30d / equity_current > 5%
    - pnl_7d > 0 (최근 1주일도 수익)
    """
    selected = []
    for t in traders:
        addr = t.get("address", "")
        if not addr:
            continue

        pnl_30d = float(t.get("pnl_30d") or 0)
        pnl_7d = float(t.get("pnl_7d") or 0)
        equity = float(t.get("equity_current") or 0)

        # ROI 계산
        roi_30d = (pnl_30d / equity * 100) if equity > 0 else 0

        # 필터 적용
        if pnl_30d > 10_000 and roi_30d > 5.0 and pnl_7d > 0:
            selected.append({
                "address": addr,
                "alias": t.get("username") or addr[:8],
                "equity_current": round(equity, 2),
                "pnl_1d": round(float(t.get("pnl_1d") or 0), 2),
                "pnl_7d": round(pnl_7d, 2),
                "pnl_30d": round(pnl_30d, 2),
                "pnl_all_time": round(float(t.get("pnl_all_time") or 0), 2),
                "roi_30d": round(roi_30d, 2),
                "oi_current": round(float(t.get("oi_current") or 0), 2),
                "volume_7d": round(float(t.get("volume_7d") or 0), 2),
                "volume_30d": round(float(t.get("volume_30d") or 0), 2),
                # 복합 점수: ROI 40% + pnl_30d 절대값 30% + pnl_7d 일관성 30%
                "score": round(roi_30d * 0.4 + (pnl_30d / 1000) * 0.3 + (pnl_7d / 1000) * 0.3, 4),
                "selected_at": int(time.time()),
            })

    # 점수 기준 내림차순 정렬
    selected.sort(key=lambda x: x["score"], reverse=True)
    return selected


async def register_to_db(traders: list, db_path: str = None):
    """선별 트레이더를 DB에 자동 등록"""
    if db_path is None:
        db_path = os.getenv("DB_PATH", os.path.join(PROJECT_ROOT, "copy_perp.db"))

    print(f"\n[DB 등록] {db_path}")
    conn = await init_db(db_path)

    registered = 0
    updated = 0
    try:
        for t in traders:
            addr = t["address"]
            alias = t["alias"]

            # INSERT OR IGNORE (신규 등록)
            async with conn.execute(
                "SELECT address FROM traders WHERE address=?", (addr,)
            ) as cur:
                existing = await cur.fetchone()

            if existing is None:
                await conn.execute(
                    "INSERT INTO traders (address, alias, created_at) VALUES (?, ?, ?)",
                    (addr, alias, int(time.time() * 1000))
                )
                registered += 1
            else:
                updated += 1

            # PnL/equity 데이터 업데이트
            await conn.execute(
                """UPDATE traders SET
                    alias=?,
                    pnl_1d=?, pnl_7d=?, pnl_30d=?, pnl_all_time=?,
                    equity=?, oi_current=?,
                    roi_30d=?,
                    last_synced=?,
                    active=1
                WHERE address=?""",
                (
                    alias,
                    t["pnl_1d"], t["pnl_7d"], t["pnl_30d"], t["pnl_all_time"],
                    t["equity_current"], t["oi_current"],
                    t["roi_30d"],
                    int(time.time() * 1000),
                    addr,
                )
            )

        await conn.commit()
        print(f"  신규 등록: {registered}명 | 업데이트: {updated}명")
    finally:
        await conn.close()

    return registered, updated


async def main(args):
    print("=" * 60)
    print("🏆 Pacifica Mainnet 트레이더 선별")
    print("=" * 60)

    # 1. 리더보드 수집
    print(f"\n[1/3] 리더보드 수집 (limit={args.limit})...")
    try:
        all_traders = fetch_leaderboard(args.limit)
        print(f"  수집: {len(all_traders)}명")
    except Exception as e:
        print(f"  오류: {e}")
        # scrapling fallback
        print("  scrapling fallback 시도...")
        try:
            from pacifica.client import _mainnet_proxy_get
            all_traders = _mainnet_proxy_get(f"leaderboard?limit={args.limit}")
            if isinstance(all_traders, dict):
                all_traders = all_traders.get("data", [])
            print(f"  scrapling 수집: {len(all_traders)}명")
        except Exception as e2:
            print(f"  scrapling도 실패: {e2}")
            return

    # 2. 필터링
    print("\n[2/3] 필터 적용 (pnl_30d>$10k AND roi>5% AND pnl_7d>0)...")
    selected = filter_traders(all_traders)
    print(f"  선별: {len(selected)}명 / {len(all_traders)}명")

    if not selected:
        print("  ⚠️ 선별 기준에 맞는 트레이더 없음 — 기준 완화 (pnl_30d>$5k, roi>2%)")
        # 기준 완화 fallback
        for t in all_traders:
            pnl_30d = float(t.get("pnl_30d") or 0)
            pnl_7d = float(t.get("pnl_7d") or 0)
            equity = float(t.get("equity_current") or 0)
            roi_30d = (pnl_30d / equity * 100) if equity > 0 else 0
            if pnl_30d > 5_000 and roi_30d > 2.0 and pnl_7d > 0:
                selected.append({
                    "address": t["address"],
                    "alias": t.get("username") or t["address"][:8],
                    "equity_current": round(equity, 2),
                    "pnl_1d": round(float(t.get("pnl_1d") or 0), 2),
                    "pnl_7d": round(pnl_7d, 2),
                    "pnl_30d": round(pnl_30d, 2),
                    "pnl_all_time": round(float(t.get("pnl_all_time") or 0), 2),
                    "roi_30d": round(roi_30d, 2),
                    "oi_current": round(float(t.get("oi_current") or 0), 2),
                    "volume_7d": round(float(t.get("volume_7d") or 0), 2),
                    "volume_30d": round(float(t.get("volume_30d") or 0), 2),
                    "score": round(roi_30d * 0.4 + (pnl_30d / 1000) * 0.3 + (pnl_7d / 1000) * 0.3, 4),
                    "selected_at": int(time.time()),
                })
        selected.sort(key=lambda x: x["score"], reverse=True)
        print(f"  완화 후 선별: {len(selected)}명")

    # 결과 출력
    print(f"\n{'순위':>4} {'주소':>16} {'pnl_30d':>12} {'roi_30d':>8} {'pnl_7d':>12} {'equity':>12}")
    print("-" * 70)
    for i, t in enumerate(selected[:20], 1):
        addr_short = t["address"][:12] + "..."
        print(f"  {i:>2}. {addr_short:>15} ${t['pnl_30d']:>10,.0f}  {t['roi_30d']:>6.1f}%  ${t['pnl_7d']:>10,.0f}  ${t['equity_current']:>10,.0f}")

    # 3. JSON 저장
    output_path = args.output or DEFAULT_OUTPUT
    result = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "network": "mainnet",
        "leaderboard_fetched": len(all_traders),
        "filter_criteria": {
            "pnl_30d_min": 10000,
            "roi_30d_min_pct": 5.0,
            "pnl_7d_min": 0,
        },
        "selected_count": len(selected),
        "traders": selected,
    }
    with open(output_path, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"\n[3/3] 저장: {output_path}")

    # DB 등록
    if not args.no_db:
        await register_to_db(selected, args.db_path)

    print("\n✅ 완료")
    return selected


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mainnet 트레이더 선별")
    parser.add_argument("--limit", type=int, default=100,
                        help="리더보드 수집 수 (10|100|25000, 기본: 100)")
    parser.add_argument("--output", type=str, default=None,
                        help="출력 JSON 경로 (기본: mainnet_selected_traders.json)")
    parser.add_argument("--no-db", action="store_true",
                        help="DB 등록 건너뜀")
    parser.add_argument("--db-path", type=str, default=None,
                        help="DB 경로 (기본: copy_perp.db)")
    args = parser.parse_args()

    asyncio.run(main(args))
