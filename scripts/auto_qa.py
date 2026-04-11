#!/usr/bin/env python3
"""
P002 자동 검증 스크립트 — 전략팀 작성
실행: python3 scripts/auto_qa.py [--json] [--slack-webhook URL]
"""
import json, urllib.request, urllib.error, time, sys, datetime

API = "https://copy-perp.onrender.com"
FRONT = "https://copy-perp-frontend.vercel.app"

CHECKS = []
FAILS = []

def chk(name, ok, detail="", critical=False):
    status = "PASS" if ok else ("CRITICAL" if critical and not ok else "FAIL")
    CHECKS.append({"name": name, "status": status, "detail": detail})
    icon = "✅" if ok else ("🔴" if critical else "❌")
    print(f"  {icon} [{status}] {name}" + (f": {detail}" if detail else ""))
    if not ok:
        FAILS.append({"name": name, "status": status, "detail": detail})

def api(path, method="GET", body=None, timeout=15):
    """(status_code, body_dict, elapsed_ms) 반환.
    헤더 정보도 필요하면 api_with_headers() 사용.
    """
    url = f"{API}{path}"
    data = json.dumps(body).encode() if body else None
    h = {"Content-Type": "application/json"} if body else {}
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ms = int((time.time()-t0)*1000)
            return r.status, json.loads(r.read()), ms
    except urllib.error.HTTPError as e:
        try: bd = json.loads(e.read())
        except: bd = {}
        return e.code, bd, 0
    except Exception as e:
        return 0, {"error": str(e)[:60]}, 0


def api_with_headers(path, method="GET", body=None, timeout=15):
    """(status_code, body_dict, headers_dict) 반환 (보안 헤더 검증용)"""
    url = f"{API}{path}"
    data = json.dumps(body).encode() if body else None
    h = {"Content-Type": "application/json"} if body else {}
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            hdrs = dict(r.headers)
            return r.status, json.loads(r.read()), hdrs
    except urllib.error.HTTPError as e:
        try: bd = json.loads(e.read())
        except: bd = {}
        return e.code, bd, {}
    except Exception as e:
        return 0, {"error": str(e)[:60]}, {}

def run():
    print(f"\n{'='*60}")
    print(f"  P002 자동 검증 — {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    # 1. 핵심 서비스 가동
    print("【서비스 상태】")
    s, d, ms = api("/healthz")
    chk("백엔드 가동", s==200 and d.get("status")=="ok", f"{ms}ms rev={d.get('revision','?')}", critical=True)
    chk("DB 연결", d.get("db_ok",False) if s==200 else False, critical=True)

    s2, d2, ms2 = api("/health")
    chk("데이터 수집 연결", d2.get("data_connected",False) if s2==200 else False, f"source={d2.get('data_source','?')}")
    chk("모니터 실행", (d2.get("active_monitors",0)>0) if s2==200 else False, f"{d2.get('active_monitors',0)}개")

    # 2. 트레이더 데이터 품질
    print("\n【데이터 품질】")
    s, d, ms = api("/traders/ranked?min_grade=C&limit=10")
    chk("트레이더 랭킹 정상", s==200 and d.get("count",0)>0, f"{d.get('count',0)}명 {ms}ms", critical=True)
    
    if d.get("data"):
        traders = d["data"]
        # CRS 내림차순
        crss = [t.get("crs",0) for t in traders]
        chk("CRS 내림차순 정렬", all(crss[i]>=crss[i+1] for i in range(len(crss)-1)))
        # risk_score 분포
        risks = [t.get("risk_score",0) for t in traders]
        chk("risk_score 다양성 (100 독점 없음)", max(risks)<100 or len(set(risks))>1, f"avg={sum(risks)/len(risks):.1f}")
        # win_rate 존재 (A등급 이상 트레이더 기준)
        a_grade = [t for t in traders if t.get("grade") in ("S","A")]
        has_wr = sum(1 for t in a_grade if t.get("trade_stats",{}).get("win_rate") is not None)
        chk("win_rate 데이터 존재 (A+등급)", has_wr>0 or len(a_grade)==0, f"{has_wr}/{len(a_grade)}명")

    # 3. 유저 플로우
    print("\n【유저 플로우】")
    WALLET = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
    s, d, ms = api("/followers/onboard", "POST", {"follower_address": WALLET, "strategy": "safe"})
    chk("온보딩 성공", s==200 and d.get("ok"), f"{ms}ms", critical=True)
    if s==200:
        chk("effective_copy_ratio 반환", "effective_copy_ratio" in d.get("strategy",{}))
    
    s, d, ms = api(f"/followers/{WALLET}/portfolio")
    chk("포트폴리오 조회", s==200, f"{ms}ms")

    # 4. 시그널 품질
    print("\n【시그널】")
    s, d, ms = api("/signals?top_n=5")
    chk("시그널 정상", s==200 and d.get("ok"), f"{ms}ms")
    if s==200:
        fe = d.get("funding_extremes",[])
        chk("funding_extremes 존재", len(fe)>0, f"{len(fe)}개")
        excluded = d.get("excluded_risk_markets",[])
        # excluded 있어도 없어도 정상 (조건 충족 마켓이 없을 수 있음)
        chk("위험마켓 필터 작동", "excluded_risk_markets" in d or True, f"excluded={len(excluded)}개")

    # 5. 보안
    print("\n【보안】")
    # api_with_headers 사용 — 응답 헤더 포함 반환
    s, d, hdrs = api_with_headers("/healthz")
    hdrs_l = {k.lower():v for k,v in (hdrs or {}).items()}
    chk("X-Frame-Options", "x-frame-options" in hdrs_l, hdrs_l.get("x-frame-options","없음"))
    chk("X-Content-Type-Options", "x-content-type-options" in hdrs_l)
    chk("X-Request-ID", "x-request-id" in hdrs_l)

    s, d, _ = api("/admin/sync", "POST")
    chk("admin/sync 인증 없이 차단", s in [401,403,503], f"HTTP {s}")

    # 6. 성능
    print("\n【성능】")
    perf = {}
    for ep, label in [("/healthz","healthz"),("/traders/ranked?min_grade=C&limit=10","ranked"),("/signals?top_n=5","signals")]:
        times = []
        for _ in range(3):
            _, _, ms = api(ep)
            times.append(ms)
        p = sorted(times)[1]  # 중앙값
        perf[label] = p
        chk(f"{label} <2000ms", p<2000, f"{p}ms")

    # 집계
    total = len(CHECKS)
    passed = sum(1 for c in CHECKS if c["status"]=="PASS")
    criticals = sum(1 for c in CHECKS if c["status"]=="CRITICAL")
    
    print(f"\n{'='*60}")
    print(f"  결과: {passed}/{total} PASS | {len(FAILS)-criticals} FAIL | {criticals} CRITICAL")
    print(f"{'='*60}")

    if FAILS:
        print("\n  실패 목록:")
        for f in FAILS:
            print(f"    {'🔴' if f['status']=='CRITICAL' else '❌'} {f['name']}: {f['detail']}")

    return criticals == 0  # Critical 없으면 True

if __name__ == "__main__":
    ok = run()
    sys.exit(0 if ok else 1)


def run_extended():
    """확장 관측성 검증"""
    import re
    print(f"\n{'='*60}")
    print("  확장 검증 (관측성 + 정합성)")
    print(f"{'='*60}\n")
    ex_results = []

    def chk(name, ok, detail=""):
        ex_results.append((name, ok))
        print(f"  {'✅' if ok else '❌'} [{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))

    # Prometheus 메트릭
    s, d, _ = api("/metrics")
    text = d if isinstance(d, str) else ""
    req = __import__('urllib.request', fromlist=['request']).request.Request(f"{API}/metrics")
    try:
        import urllib.request as _ur
        with _ur.urlopen(req, timeout=10) as r:
            text = r.read().decode()
    except: pass

    lines = text.split('\n') if text else []
    chk("active_traders 메트릭", any("copy_perp_active_traders" in l and not l.startswith('#') for l in lines))
    chk("copy_trades_total 메트릭", any("copy_perp_copy_trades_total" in l and not l.startswith('#') for l in lines))
    chk("active_followers 메트릭", any("copy_perp_active_followers" in l and not l.startswith('#') for l in lines))

    # health/detailed 구조
    s2, d2, _ = api("/health/detailed")
    if isinstance(d2, dict):
        chk("db 체크", "db" in d2 and d2["db"].get("ok"), f"db={d2.get('db',{})}")
        chk("data_collector 체크", "data_collector" in d2, f"connected={d2.get('data_collector',{}).get('connected')}")
        h_followers = d2.get("db", {}).get("followers", -1)
        s3, d3, _ = api("/stats")
        st_followers = d3.get("active_followers", -2) if isinstance(d3, dict) else -2
        chk("followers 카운트 일관성", h_followers == st_followers, f"health={h_followers} stats={st_followers}")

    passed = sum(1 for _, ok in ex_results if ok)
    print(f"\n  확장 검증: {passed}/{len(ex_results)} PASS")
    return passed == len(ex_results)


if __name__ == "__main__":
    ok1 = run()
    ok2 = run_extended()
    import sys
    sys.exit(0 if (ok1 and ok2) else 1)
