#!/usr/bin/env python3
"""
Tier 1 트레이더 17명 자동 등록 스크립트
POST /traders API로 등록 + PositionMonitor 자동 시작

Usage:
    python3 scripts/register_traders.py [--host http://localhost:8001]
"""
import sys, json, time, argparse, urllib.request, ssl

TIER1 = [
    "EcX5xSDT45Nvhi2gMTjTnhF3KT2w4sPF54esEZS3hwZu",
    "4UBH19qUbXEaqyz9fKrFHuvj8BPMoM87H71s1YPKyGYq",
    "A6VY4ZBUohgSLkwMuDwDvAnzgiXFB1eTDzaixyitPJep",
    "7C3sXQ6KvXJLkYGwzjNy2BHpkfEnRHzzfVAgUS64CDEd",
    "7gV81bz99MUBVb2aLYxW7MG1RMDdRdJYTPyC2syjba8y",
    "3rXoG6i55P7D1Q3tYsB7Unds8nBtKh7vH5VUyMDpWkSe",
    "E1vabqxiuUfB29BAwLppTLLNMAq6HJqp7gSz1NiYwWz7",
    "9XCVb4SQeNMGT4bGR7cTPMGHF2i2SqEzk5KT6Hp48qen",
    "5BPd5WYVvDE2kHMjzGmLHMaAorSm8bEfERcsycg5GCAD",
    "7kDTQZPTnaCidXZwEhkoLSia5BKb7zhQ6CmBX2g1RiG3",
    "8r5HRJeSScGX1TB9D2FZ45xEDspm1qfK4CTuaZvqe7en",
    "EYhhf8u9M6kN9tCRVgd2Jki9fJm3XzJRnTF9k5eBC1q1",
    "HcG1FFVf6bW5oEpkU8m3f2Ev2FzFPB5dkdPHFHQtieMQ",
    "A4XbPsH59TWjp6vx3QnY8sCb26ew4pBYkYc8Vk4kpbqk",
    "FuHMGqdrn77u944FSYvg9VTw3sD5RVeYS1ezLpGaFes7",
    "DThxt2yhDvJv9KU9bPMuKsd7vcwdDtaRtuh4NvohutQi",
    "AF5a28meHjecM4dNy8FssFHquWJVv4BK1e5Z8ipRkDgT",
]

def register(host: str, addr: str) -> dict:
    url = f"{host}/traders"
    body = json.dumps({"address": addr, "alias": f"TIER1-{addr[:8]}"}).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode()[:100]}"}
    except Exception as e:
        return {"error": str(e)}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="http://localhost:8001")
    args = parser.parse_args()

    print(f"=== Tier1 트레이더 {len(TIER1)}명 등록 → {args.host} ===\n")
    ok = fail = 0
    for i, addr in enumerate(TIER1, 1):
        r = register(args.host, addr)
        if "error" in r:
            print(f"  [{i:2}] ❌ {addr[:16]}... {r['error']}")
            fail += 1
        else:
            monitoring = "✅ 모니터링" if r.get("monitoring") else "⚠️  모니터없음"
            print(f"  [{i:2}] ✅ {addr[:16]}... {monitoring}")
            ok += 1
        time.sleep(0.3)  # API rate limit 방지

    print(f"\n등록 완료: {ok}명 성공 / {fail}명 실패")
    
    # 최종 상태 확인
    try:
        with urllib.request.urlopen(f"{args.host}/health", timeout=5) as r:
            h = json.loads(r.read())
            print(f"active_monitors: {h.get('active_monitors')}")
    except Exception:
        pass

if __name__ == "__main__":
    main()
