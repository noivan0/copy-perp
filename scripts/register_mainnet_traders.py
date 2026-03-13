#!/usr/bin/env python3
"""
Mainnet 상위 트레이더 DB 등록
mainnet_traders.json 수집 후 실행

Usage:
    python3 scripts/register_mainnet_traders.py [--limit 20]
"""
import sys, os, json, time, urllib.request, argparse
sys.path.insert(0, '.')
from dotenv import load_dotenv; load_dotenv('.env')

API = "http://localhost:8001"

def post(path, body):
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{API}{path}", data=payload,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--min-pnl", type=float, default=1000, help="최소 30일 PnL (USDC)")
    args = parser.parse_args()

    data_path = "mainnet_traders.json"
    if not os.path.exists(data_path):
        print(f"오류: {data_path} 없음. 먼저 mainnet 리더보드 수집 필요.")
        sys.exit(1)

    traders = json.load(open(data_path))
    if isinstance(traders, dict) and "top20" in traders:
        traders = traders["top20"]

    # 필터: 양수 PnL + 최소 거래량
    filtered = [
        t for t in traders
        if float(t.get("pnl_30d", 0)) >= args.min_pnl
           and float(t.get("volume_30d", 0)) > 0
    ]
    filtered.sort(key=lambda x: float(x["pnl_30d"]), reverse=True)
    selected = filtered[:args.limit]

    print(f"Mainnet 트레이더 등록: {len(selected)}명 (필터 후 {len(filtered)}명 중)")
    ok, fail = 0, 0
    for i, t in enumerate(selected):
        addr = t["address"]
        pnl30 = float(t.get("pnl_30d", 0))
        alias = f"MAIN-TOP{i+1}"
        result = post("/traders", {
            "address": addr,
            "alias": alias,
            "roi_30d": round(pnl30 / max(float(t.get("equity_current", 1)), 1) * 100, 2),
            "pnl_30d": pnl30,
            "win_rate": float(t.get("win_rate", 0)),
            "volume_30d": float(t.get("volume_30d", 0)),
        })
        if "error" not in str(result).lower():
            ok += 1
            print(f"  ✅ [{alias}] {addr[:16]}... pnl30=${pnl30:,.0f}")
        else:
            fail += 1
            print(f"  ❌ [{alias}] {addr[:16]}... {result}")
        time.sleep(0.1)

    print(f"\n완료: 성공 {ok}명 / 실패 {fail}명")

if __name__ == "__main__":
    main()
