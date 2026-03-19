"""
Mainnet 장기 추적 시스템
- 주기적으로 mainnet 리더보드 + 거래내역 수집
- pnl_records / equity_snapshots 누적
- 신뢰도 기준 자동 산출 (실측 데이터 역산)
- cron 또는 supervisord로 주기 실행 가능
"""

import sqlite3, json, time, math, urllib.parse, logging
import requests, urllib3
from datetime import datetime, date

urllib3.disable_warnings()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

PROXY   = "https://api.codetabs.com/v1/proxy/?quest="
BASE    = "https://api.pacifica.fi/api/v1"
DB_PATH = "copy_perp.db"
POLL_INTERVAL_SEC = 900  # 15분

# ── 신뢰도 기준 (실측 역산 후 확정값) ────────────────────────
TIER_A = {"carp": 55, "pf": 2.5, "kelly": 0.10, "cnt": 100, "pnl_all_min": 10_000}
TIER_B = {"carp": 35, "pf": 1.5, "kelly": 0.05, "cnt": 50,  "pnl_all_min": 1_000}

def pm_get(path, params=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        r = requests.get(PROXY + urllib.parse.quote(url), timeout=20)
        return r.json() if r.ok else {}
    except Exception as e:
        logger.warning(f"pm_get 오류 {path}: {e}")
        return {}

def calc_trade_stats(trades: list) -> dict | None:
    positions = {}
    pnl_list  = []

    for t in sorted(trades, key=lambda x: x.get("created_at", 0)):
        sym   = t.get("symbol", "?")
        side  = t.get("side", "")
        amt   = float(t.get("amount", 0) or 0)
        price = float(t.get("entry_price", t.get("price", 0)) or 0)
        ts    = t.get("created_at", 0)

        if t.get("cause") == "liquidation":
            positions.pop(sym, None)
            continue

        pos = positions.get(sym)
        if pos is None:
            positions[sym] = {"side": side, "size": amt, "price": price, "ts": ts}
        elif pos["side"] != side:
            close = min(pos["size"], amt)
            if pos["side"] == "bid":
                pnl = (price - pos["price"]) * close
            else:
                pnl = (pos["price"] - price) * close
            pnl_list.append({"pnl": pnl, "hold": (ts - pos["ts"]) / 1000})
            remaining = pos["size"] - close
            positions[sym] = ({"side": side, "size": remaining, "price": pos["price"], "ts": pos["ts"]}
                              if remaining > 1e-8 else None)
            if positions[sym] is None:
                positions.pop(sym, None)
        else:
            ns = pos["size"] + amt
            np_ = (pos["price"] * pos["size"] + price * amt) / ns
            positions[sym] = {"side": side, "size": ns, "price": np_, "ts": pos["ts"]}

    if not pnl_list:
        return None

    cnt  = len(pnl_list)
    wins = sum(1 for p in pnl_list if p["pnl"] > 0)
    gp   = sum(p["pnl"] for p in pnl_list if p["pnl"] > 0)
    gl   = abs(sum(p["pnl"] for p in pnl_list if p["pnl"] <= 0))
    wr   = wins / cnt
    pf   = gp / gl if gl > 0 else 9.99
    aw   = gp / wins if wins > 0 else 0
    al   = gl / (cnt - wins) if (cnt - wins) > 0 else 1
    po   = aw / al if al > 0 else 0
    kelly = wr - (1 - wr) / po if po > 0 else -1.0
    avg_hold = sum(p["hold"] for p in pnl_list) / cnt

    return {
        "closed_cnt": cnt,
        "win_rate": round(wr * 100, 2),
        "profit_factor": round(pf, 4),
        "payoff_ratio": round(po, 3),
        "kelly": round(kelly, 4),
        "avg_hold_min": round(avg_hold / 60, 1),
        "total_pnl": round(sum(p["pnl"] for p in pnl_list), 4),
        "gross_profit": round(gp, 4),
        "gross_loss": round(gl, 4),
    }

def carp_score(pnl_all, pnl_30d, equity, stats) -> float:
    if not stats:
        return 0.0
    wr    = stats["win_rate"] / 100
    pf    = stats["profit_factor"]
    kelly = stats["kelly"]
    cnt   = stats["closed_cnt"]

    psr = min(wr * 1.5, 1.0) if cnt >= 50 else (wr * 0.8 if cnt >= 20 else wr * 0.5)
    c   = 30 * psr
    alpha = min((pf - 1) * 10, 25) if pf >= 1 and pnl_all > 0 else 0
    risk  = max(kelly * 25, 0) if kelly > 0 else 0
    pers  = min(math.log10(max(cnt, 1)) / 2.5 * 20, 20) if pnl_all > 0 else 0
    return round(min(c + alpha + risk + pers, 100), 1)

def tier_label(score, stats, pnl_all) -> str:
    if not stats:
        return "D"
    s = stats
    if (score >= TIER_A["carp"] and s["profit_factor"] >= TIER_A["pf"]
            and s["kelly"] >= TIER_A["kelly"] and s["closed_cnt"] >= TIER_A["cnt"]
            and pnl_all >= TIER_A["pnl_all_min"]):
        return "A"
    if (score >= TIER_B["carp"] and s["profit_factor"] >= TIER_B["pf"]
            and s["kelly"] >= TIER_B["kelly"] and s["closed_cnt"] >= TIER_B["cnt"]
            and pnl_all >= TIER_B["pnl_all_min"]):
        return "B"
    if s["kelly"] > 0 and s["profit_factor"] >= 1.0:
        return "C"
    return "D"

def fetch_and_store():
    """메인 수집 루프 1회"""
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()
    now_ms = int(time.time() * 1000)
    today  = date.today().isoformat()

    # 1. 리더보드 수집
    lb = pm_get("/leaderboard", {"limit": 100, "orderBy": "PNL", "timePeriod": "ALL"})
    all_traders = lb.get("data", []) or []
    profitable = [t for t in all_traders
                  if float(t.get("pnl_all_time", 0) or 0) > 0]
    logger.info(f"리더보드: {len(all_traders)}명 → 수익 {len(profitable)}명")

    # 2. 트레이더별 실거래 분석
    analyzed = []
    for t in profitable[:30]:  # 상위 30명
        addr  = t.get("address", "")
        alias = t.get("alias", addr[:12])
        pnl_all = float(t.get("pnl_all_time", 0) or 0)
        pnl_30  = float(t.get("pnl_30d", 0) or 0)
        equity  = float(t.get("equity", t.get("account_equity", 0)) or 0)
        vol     = float(t.get("volume", 0) or 0)

        # 기존 분석 결과 재사용 (오늘 이미 했으면 스킵)
        cur.execute("SELECT last_synced FROM traders WHERE address=?", (addr,))
        row = cur.fetchone()
        if row and row[0] and (now_ms - row[0]) < 3600_000:  # 1시간 내 캐시
            cur.execute("SELECT win_rate, sharpe, tier FROM traders WHERE address=?", (addr,))
            r = cur.fetchone()
            if r and r[1]:  # sharpe(=CARP/100) 존재할 때만 캐시 사용
                analyzed.append({
                    "address": addr, "alias": alias,
                    "pnl_all": pnl_all, "pnl_30d": pnl_30, "equity": equity,
                    "carp": round(float(r[1]) * 100, 1), "tier": r[2] or "C",
                    "win_rate": float(r[0]) if r[0] else 0,
                })
                continue

        # 거래 내역 조회
        try:
            d = pm_get("/trades/history", {"address": addr, "limit": 500})
            trades = d.get("data", []) or []
            time.sleep(0.4)
        except Exception:
            trades = []

        stats = calc_trade_stats(trades)
        score = carp_score(pnl_all, pnl_30, equity, stats)
        tier  = tier_label(score, stats, pnl_all)

        wr = stats["win_rate"] if stats else 0
        pf = stats["profit_factor"] if stats else 0

        # DB 저장
        roi_30 = round(pnl_30 / max(equity, 1) * 100, 2) if equity > 0 else 0
        cur.execute("""
            INSERT OR REPLACE INTO traders
            (address, alias, roi_30d, pnl_30d, pnl_all_time, equity,
             volume_30d, win_rate, sharpe, tier, active, last_synced)
            VALUES (?,?,?,?,?,?,?,?,?,?,1,?)
        """, (addr, alias, roi_30, pnl_30, pnl_all, equity,
              vol, wr, score / 100, tier, now_ms))

        analyzed.append({
            "address": addr, "alias": alias,
            "pnl_all": pnl_all, "pnl_30d": pnl_30, "equity": equity,
            "carp": score, "tier": tier, "win_rate": wr,
            "pf": pf, "stats": stats,
        })
        logger.info(f"  {alias}: CARP={score} Tier={tier} WR={wr}% PF={pf}")

    conn.commit()

    # 3. 신뢰도 기준 현황 집계
    tier_counts = {"A": 0, "B": 0, "C": 0, "D": 0}
    for a in analyzed:
        tier_counts[a.get("tier", "D")] = tier_counts.get(a.get("tier", "D"), 0) + 1

    # 4. 플랫폼 전체 성과 스냅샷 (daily_stats에 플랫폼 레벨 기록)
    cur.execute("SELECT COALESCE(SUM(net_pnl),0), COUNT(*), SUM(CASE WHEN net_pnl>0 THEN 1 ELSE 0 END) FROM pnl_records")
    r = cur.fetchone()
    platform_pnl = float(r[0])
    platform_cnt = int(r[1])
    platform_wins = int(r[2])

    # 5. 결과 저장 → mainnet_tracker_log.json 누적
    snapshot = {
        "ts": now_ms,
        "dt": datetime.now().isoformat(),
        "leaderboard_total": len(all_traders),
        "profitable": len(profitable),
        "analyzed": len(analyzed),
        "tier_counts": tier_counts,
        "platform_pnl": platform_pnl,
        "platform_trades": platform_cnt,
        "platform_wr": round(platform_wins / platform_cnt * 100, 1) if platform_cnt else 0,
        "top10": [{
            "alias": a["alias"],
            "pnl_all": a["pnl_all"],
            "pnl_30d": a["pnl_30d"],
            "carp": a["carp"],
            "tier": a["tier"],
        } for a in sorted(analyzed, key=lambda x: x["carp"], reverse=True)[:10]],
    }

    # 로그 누적
    try:
        with open("mainnet_tracker_log.json", "r") as f:
            log = json.load(f)
    except Exception:
        log = []
    log.append(snapshot)
    with open("mainnet_tracker_log.json", "w") as f:
        json.dump(log[-1000:], f, indent=2, ensure_ascii=False)  # 최대 1000개 유지

    conn.close()
    return snapshot

def print_summary(snap):
    SEP = "=" * 68
    print(f"\n{SEP}")
    print(f"  Mainnet Tracker 수집 완료 | {snap['dt'][:16]}")
    print(SEP)
    print(f"  리더보드: {snap['leaderboard_total']}명 | 수익: {snap['profitable']}명 | 분석: {snap['analyzed']}명")
    print(f"  Tier A:{snap['tier_counts']['A']} / B:{snap['tier_counts']['B']} / C:{snap['tier_counts']['C']} / D:{snap['tier_counts']['D']}")
    print(f"  플랫폼 누적 PnL: ${snap['platform_pnl']:+.2f} ({snap['platform_trades']}건, WR {snap['platform_wr']}%)")
    print(f"\n  CARP Top 10:")
    print(f"  {'#':<3} {'트레이더':<14} {'PnL_all':>12} {'PnL_30d':>10} {'CARP':>6} {'Tier':>5}")
    for i, t in enumerate(snap["top10"], 1):
        print(f"  {i:<3} {t['alias']:<14} ${t['pnl_all']:>10,.0f} ${t['pnl_30d']:>8,.0f} {t['carp']:>6.0f}   {t['tier']}")
    print(SEP)

def run_once():
    snap = fetch_and_store()
    print_summary(snap)
    return snap

def run_loop():
    logger.info(f"Mainnet Tracker 시작 (간격: {POLL_INTERVAL_SEC}초)")
    while True:
        try:
            snap = fetch_and_store()
            print_summary(snap)
        except Exception as e:
            logger.error(f"수집 오류: {e}")
        logger.info(f"다음 수집까지 {POLL_INTERVAL_SEC}초 대기...")
        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    import sys
    if "--loop" in sys.argv:
        run_loop()
    else:
        run_once()
