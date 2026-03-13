"""
Mainnet 상위 트레이더 등록 스크립트
mainnet_traders.json에서 상위 20명을 읽어 POST /traders API로 등록
alias: MAIN-TOP{순위}

사용법:
  python3 scripts/register_mainnet_traders.py [--api-url http://localhost:8000]
"""
import json
import sys
import os
import argparse
import urllib.request
import urllib.error

# 프로젝트 루트를 path에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def load_mainnet_traders(filepath: str = "mainnet_traders.json") -> list:
    """mainnet_traders.json 로드"""
    path = filepath if os.path.isabs(filepath) else os.path.join(
        os.path.dirname(os.path.dirname(__file__)), filepath
    )
    with open(path, "r") as f:
        traders = json.load(f)
    return traders


def register_trader(api_url: str, trader: dict, rank: int) -> dict:
    """POST /traders 로 트레이더 등록"""
    alias = f"MAIN-TOP{rank}"
    payload = {
        "address": trader["address"],
        "alias": alias,
        "total_pnl": float(trader.get("pnl_all_time", 0) or 0),
        "pnl_1d": float(trader.get("pnl_1d", 0) or 0),
        "pnl_7d": float(trader.get("pnl_7d", 0) or 0),
        "pnl_30d": float(trader.get("pnl_30d", 0) or 0),
        "equity": float(trader.get("equity_current", 0) or 0),
        "network": "mainnet",
        "source": "mainnet_leaderboard",
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{api_url}/traders",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return {"status": "ok", "alias": alias, "code": resp.status,
                    "response": json.loads(resp.read().decode())}
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        # 이미 등록된 경우(409 conflict) 는 정상으로 처리
        if e.code == 409 or "already" in body.lower():
            return {"status": "exists", "alias": alias, "code": e.code}
        return {"status": "error", "alias": alias, "code": e.code, "error": body[:200]}
    except Exception as ex:
        return {"status": "error", "alias": alias, "error": str(ex)}


def main():
    parser = argparse.ArgumentParser(description="Mainnet 상위 트레이더 등록")
    parser.add_argument("--api-url", default=os.getenv("COPY_PERP_API_URL", "http://localhost:8000"),
                        help="Copy Perp API URL (default: http://localhost:8000)")
    parser.add_argument("--file", default="mainnet_traders.json",
                        help="트레이더 JSON 파일 경로 (default: mainnet_traders.json)")
    parser.add_argument("--dry-run", action="store_true", help="실제 등록 없이 출력만")
    args = parser.parse_args()

    print(f"📡 API: {args.api_url}")
    print(f"📁 파일: {args.file}")
    if args.dry_run:
        print("🔍 DRY RUN 모드 — 실제 등록 안함\n")

    traders = load_mainnet_traders(args.file)
    print(f"✅ {len(traders)}명 로드 완료\n")
    print(f"{'순위':<6} {'주소':>46} {'PnL_30d':>15} {'상태'}")
    print("-" * 85)

    ok_count = 0
    err_count = 0

    for t in traders:
        rank = t.get("rank", traders.index(t) + 1)
        addr = t["address"]
        pnl30 = float(t.get("pnl_30d", 0) or 0)
        alias = f"MAIN-TOP{rank}"

        if args.dry_run:
            print(f"{rank:<6} {addr:>46} {pnl30:>15,.2f}  [{alias}] (dry-run)")
            ok_count += 1
            continue

        result = register_trader(args.api_url, t, rank)
        status = result["status"]
        if status in ("ok", "exists"):
            ok_count += 1
            mark = "✅" if status == "ok" else "♻️ "
        else:
            err_count += 1
            mark = "❌"
        err_info = f" | {result.get('error', '')[:50]}" if status == "error" else ""
        print(f"{rank:<6} {addr:>46} {pnl30:>15,.2f}  {mark} [{alias}]{err_info}")

    print("-" * 85)
    print(f"\n완료: ✅ {ok_count}명 등록 | ❌ {err_count}명 실패")
    if err_count > 0:
        print("⚠️  API 서버가 실행 중인지 확인하세요: uvicorn api.main:app --reload")


if __name__ == "__main__":
    main()
