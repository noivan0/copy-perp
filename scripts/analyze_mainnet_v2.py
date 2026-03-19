"""
Mainnet 트레이더 분석 v2 — 실거래 기반 신뢰도 스코어링
"""
import requests, urllib3, json, urllib.parse, math, sqlite3, time
from datetime import datetime

urllib3.disable_warnings()
PROXY = "https://api.codetabs.com/v1/proxy/?quest="
BASE  = "https://api.pacifica.fi/api/v1"
DB_PATH = "copy_perp.db"

def pm_get(path, params=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    r = requests.get(PROXY + urllib.parse.quote(url), timeout=20)
    if r.ok:
        return r.json()
    return {}

def calc_stats(trades):
    """체결 내역 → win_rate, profit_factor, avg_hold_sec"""
    positions = {}  # symbol → {side, entry, size, opened_at}
    pnl_list = []

    for t in sorted(trades, key=lambda x: x.get("created_at", 0)):
        sym   = t.get("symbol", "?")
        side  = t.get("side", "")          # bid/ask
        amt   = float(t.get("amount", 0) or 0)
        price = float(t.get("entry_price", t.get("price", 0)) or 0)
        ts    = t.get("created_at", 0)
        cause = t.get("cause", "")

        if cause == "liquidation":
            positions.pop(sym, None)
            continue

        pos = positions.get(sym)
        if pos is None:
            positions[sym] = {"side": side, "size": amt, "price": price, "ts": ts}
        elif pos["side"] != side:
            # 청산
            entry = pos["price"]
            close_size = min(pos["size"], amt)
            if pos["side"] == "bid":
                pnl = (price - entry) * close_size
            else:
                pnl = (entry - price) * close_size
            hold = (ts - pos["ts"]) / 1000
            pnl_list.append({"pnl": pnl, "hold": hold})
            remaining = pos["size"] - close_size
            if remaining > 1e-8:
                positions[sym]["size"] = remaining
            else:
                positions.pop(sym, None)
        else:
            # 추가
            old = pos
            new_size  = old["size"] + amt
            new_price = (old["price"] * old["size"] + price * amt) / new_size
            positions[sym] = {"side": side, "size": new_size, "price": new_price, "ts": old["ts"]}

    if not pnl_list:
        return None

    wins = sum(1 for p in pnl_list if p["pnl"] > 0)
    total = len(pnl_list)
    gross_profit = sum(p["pnl"] for p in pnl_list if p["pnl"] > 0)
    gross_loss   = abs(sum(p["pnl"] for p in pnl_list if p["pnl"] <= 0))
    avg_hold     = sum(p["hold"] for p in pnl_list) / total

    wr = wins / total
    pf = gross_profit / gross_loss if gross_loss > 0 else 9.99
    avg_win  = gross_profit / wins if wins > 0 else 0
    avg_loss = gross_loss / (total - wins) if (total - wins) > 0 else 1
    payoff = avg_win / avg_loss if avg_loss > 0 else 0
    kelly  = wr - (1 - wr) / payoff if payoff > 0 else -1.0

    return {
        "closed_cnt": total,
        "win_rate": round(wr * 100, 1),
        "profit_factor": round(pf, 3),
        "payoff_ratio": round(payoff, 2),
        "kelly": round(kelly, 3),
        "avg_hold_min": round(avg_hold / 60, 1),
        "total_pnl": round(sum(p["pnl"] for p in pnl_list), 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
    }

def score_trader(leaderboard_row, stats):
    """CARP 신뢰도 점수 (0~100)"""
    pnl_all = float(leaderboard_row.get("pnl_all_time", 0) or 0)
    pnl_30  = float(leaderboard_row.get("pnl_30d", 0) or 0)
    equity  = float(leaderboard_row.get("equity", leaderboard_row.get("account_equity", 1)) or 1)
    vol     = float(leaderboard_row.get("volume", 0) or 0)

    if not stats:
        return 0, "데이터 부족"

    wr = stats["win_rate"] / 100
    pf = stats["profit_factor"]
    kelly = stats["kelly"]
    cnt = stats["closed_cnt"]

    # Consistency (30점): PSR proxy
    psr = min(wr * 2, 1.0) if cnt >= 20 else wr
    c = 30 * psr

    # Alpha (25점): PF + PnL 방향
    pf_score = min((pf - 1) * 12.5, 25) if pf >= 1 else 0
    alpha = pf_score if pnl_all > 0 else 0

    # Risk (25점): Kelly
    risk = max(kelly * 25, 0) if kelly > 0 else 0

    # Persistence (20점): 거래 건수 + 누적 PnL 양수
    persistence = min(math.log10(max(cnt, 1)) / 3 * 20, 20) if pnl_all > 0 else 0

    total = c + alpha + risk + persistence
    reason = f"WR={stats['win_rate']}% PF={pf:.2f} Kelly={kelly:.3f} cnt={cnt}"
    return round(min(total, 100), 1), reason

def main():
    print("Mainnet 리더보드 조회 중...")
    lb = pm_get("/leaderboard", {"limit": 100, "orderBy": "PNL", "timePeriod": "ALL"})
    all_traders = lb.get("data", []) or []

    # 수익 양수만 1차 필터
    candidates = [t for t in all_traders
                  if float(t.get("pnl_all_time", 0) or 0) > 0
                  and float(t.get("pnl_30d", 0) or 0) > 0]
    print(f"전체: {len(all_traders)}명 → 수익 양수: {len(candidates)}명")

    results = []
    for t in candidates[:20]:
        addr  = t.get("address", "")
        alias = t.get("alias", addr[:12])
        pnl_all = float(t.get("pnl_all_time", 0) or 0)
        pnl_30  = float(t.get("pnl_30d", 0) or 0)
        equity  = float(t.get("equity", t.get("account_equity", 0)) or 0)

        # 실거래 내역 조회
        try:
            d = pm_get("/trades/history", {"address": addr, "limit": 500})
            trades = d.get("data", []) or []
            time.sleep(0.3)  # rate limit
        except Exception as e:
            trades = []

        stats = calc_stats(trades)
        carp, reason = score_trader(t, stats)

        results.append({
            "address": addr,
            "alias": alias,
            "pnl_all": pnl_all,
            "pnl_30d": pnl_30,
            "equity": equity,
            "trades_fetched": len(trades),
            "stats": stats,
            "carp_score": carp,
            "reason": reason,
        })

    # CARP 점수 정렬
    results.sort(key=lambda x: x["carp_score"], reverse=True)

    # 출력
    SEP = "=" * 72
    print(f"\n{SEP}")
    print("  Mainnet 트레이더 신뢰도 랭킹 (CARP Score 기준)")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M KST')}")
    print(SEP)
    print(f"  {'#':<3} {'트레이더':<14} {'PnL_all':>10} {'PnL_30d':>10} {'WR':>6} {'PF':>6} {'Kelly':>7} {'cnt':>4} {'CARP':>6}")
    print(f"  {'-'*70}")

    tier_a, tier_b, tier_c = [], [], []
    for i, r in enumerate(results, 1):
        s = r["stats"]
        wr_str   = f"{s['win_rate']:.0f}%" if s else "N/A"
        pf_str   = f"{s['profit_factor']:.2f}" if s else "N/A"
        kelly_str = f"{s['kelly']:.3f}" if s else "N/A"
        cnt_str  = str(s["closed_cnt"]) if s else "0"
        star = "★" if r["carp_score"] >= 60 else ("◆" if r["carp_score"] >= 35 else "▶")
        print(f"  {star}{i:<2} {r['alias']:<14} ${r['pnl_all']:>9,.0f} ${r['pnl_30d']:>9,.0f} "
              f"{wr_str:>6} {pf_str:>6} {kelly_str:>7} {cnt_str:>4} {r['carp_score']:>5.0f}")

        if r["carp_score"] >= 60:
            tier_a.append(r)
        elif r["carp_score"] >= 35:
            tier_b.append(r)
        else:
            tier_c.append(r)

    print(f"\n  ★ TIER A (CARP≥60): {len(tier_a)}명  ◆ TIER B (≥35): {len(tier_b)}명  ▶ TIER C (<35): {len(tier_c)}명")

    # TIER A 상세
    if tier_a:
        print(f"\n┌─ TIER A 트레이더 상세 (복사 추천) ─────────────────────────────┐")
        for r in tier_a:
            s = r["stats"]
            print(f"│  {r['alias']} ({r['address'][:20]}...)")
            print(f"│    pnl_all=${r['pnl_all']:,.0f} pnl_30d=${r['pnl_30d']:,.0f} equity=${r['equity']:,.0f}")
            if s:
                print(f"│    WR={s['win_rate']}% PF={s['profit_factor']:.2f} Payoff={s['payoff_ratio']:.1f}x "
                      f"Kelly={s['kelly']:.3f} hold={s['avg_hold_min']:.0f}min cnt={s['closed_cnt']}")
            print(f"│    CARP={r['carp_score']} | {r['reason']}")
        print(f"└{'─'*68}┘")

    # DB 업데이트
    update_db(results)

    # JSON 저장
    out = {
        "generated_at": datetime.now().isoformat(),
        "total_analyzed": len(results),
        "tier_a": len(tier_a),
        "tier_b": len(tier_b),
        "tier_c": len(tier_c),
        "traders": [{
            "address": r["address"],
            "alias": r["alias"],
            "pnl_all": r["pnl_all"],
            "pnl_30d": r["pnl_30d"],
            "carp_score": r["carp_score"],
            "stats": r["stats"],
        } for r in results]
    }
    with open("mainnet_carp_analysis.json", "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\n결과 저장: mainnet_carp_analysis.json")

def update_db(results):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    now_ms = int(time.time() * 1000)
    for r in results:
        s = r["stats"] or {}
        tier = ("A" if r["carp_score"] >= 60 else
                "B" if r["carp_score"] >= 35 else "C")
        cur.execute("""
            INSERT OR REPLACE INTO traders
            (address, alias, roi_30d, pnl_30d, pnl_all_time, equity,
             win_rate, sharpe, tier, active, last_synced)
            VALUES (?,?,?,?,?,?,?,?,?,1,?)
        """, (
            r["address"], r["alias"],
            round(r["pnl_30d"] / max(r["equity"], 1) * 100, 2),
            r["pnl_30d"], r["pnl_all"],
            r["equity"],
            s.get("win_rate", 0),
            r["carp_score"] / 100,  # sharpe 컬럼에 CARP 정규화값 임시 저장
            tier,
            now_ms,
        ))
    conn.commit()
    conn.close()
    print(f"DB 업데이트: {len(results)}명")

if __name__ == "__main__":
    main()
