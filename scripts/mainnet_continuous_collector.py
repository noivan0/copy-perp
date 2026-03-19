"""
Mainnet 장기 데이터 수집기
- 15분마다 mainnet 트레이더 거래내역 수집 → DB 누적
- copy_trades와 동일한 구조로 mainnet_trades 테이블에 기록
- 시간이 쌓일수록 WR/PF/Kelly 신뢰도 증가
- 독립 실행 or supervisord 관리
"""

import sqlite3, json, time, math, urllib.parse, logging, sys
import requests, urllib3
from datetime import datetime, date

urllib3.disable_warnings()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("/tmp/mainnet-collector.log"),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger(__name__)

PROXY   = "https://api.codetabs.com/v1/proxy/?quest="
BASE    = "https://api.pacifica.fi/api/v1"
DB_PATH = "copy_perp.db"
INTERVAL_SEC = 900  # 15분

# 추적 대상 (CARP 기준 상위 + 누적 PnL 검증된 트레이더)
WATCH_TRADERS = [
    # alias, address, pnl_all
    ("4TYEjn9P",  "4TYEjn9PSpxoBNBXWgvUGaqQ8B4sNHRcLUEbA9mHzPfZ", 1_965_189),
    ("YjCD9Gek",  "YjCD9Gek6MVY9t3MLEGYYdZLeaF6MZrpgZraayWsv9E",  1_584_118),
    ("6ZjWoJKe",  "6ZjWoJKeD88JqREHR2R4yS5m4Y3UGiNtqsUWdGXeVeQu",   234_460),
    ("E8j5xSbG",  "E8j5xSbGXEWt68RRNQG9EXwRwjVtAzuHCPKhP9ckGCQq",   199_071),
    ("G5GWsm3f",  "G5GWsm3f9C2rztJNN4o5FYnmH8Bj7BPJXX6sQWDwwi6M",   189_008),
    ("3iKDU1jU",  "3iKDU1jUU1KrJXFkYuQBRUALSFKbnWUFjx1o8E7VqxhG",    29_863),
    ("5RX2DD42",  "5RX2DD425DHjJHJWYSiJcFh7BsRb6b66UFYSmB2jJBHs",    57_111),
    ("Ep1d8JdF",  "Ep1d8JdFw4FnB85XtSa4bFMR5eCKnBiSG7aMxapFLNkn",    60_617),
    ("HtC4WT6J",  "HtC4WT6JhKz8eojNbkpAv16j5mB6JBj3y8EVbuVzHkCZ",   442_281),
    ("GTU92nBC",  "GTU92nBC8LMyt9W4Qqc319BFR1vpkNNPAbt4QCnX7kZ6",    40_685),
]

def pm_get(path, params=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        r = requests.get(PROXY + urllib.parse.quote(url), timeout=20)
        return r.json() if r.ok else {}
    except Exception as e:
        logger.warning(f"pm_get 오류: {e}")
        return {}

def init_mainnet_tables(conn):
    """mainnet 전용 테이블 생성"""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS mainnet_trades (
        id              TEXT PRIMARY KEY,
        trader_address  TEXT NOT NULL,
        trader_alias    TEXT,
        symbol          TEXT,
        side            TEXT,
        amount          REAL,
        entry_price     REAL,
        event_type      TEXT,
        cause           TEXT,
        created_at      INTEGER,
        collected_at    INTEGER,
        UNIQUE(trader_address, id)
    );

    CREATE TABLE IF NOT EXISTS mainnet_stats (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        trader_address  TEXT NOT NULL,
        trader_alias    TEXT,
        snapshot_date   TEXT NOT NULL,
        snapshot_ts     INTEGER NOT NULL,
        pnl_all_time    REAL DEFAULT 0,
        pnl_30d         REAL DEFAULT 0,
        equity          REAL DEFAULT 0,
        volume          REAL DEFAULT 0,
        closed_cnt      INTEGER DEFAULT 0,
        win_rate        REAL DEFAULT 0,
        profit_factor   REAL DEFAULT 0,
        payoff_ratio    REAL DEFAULT 0,
        kelly           REAL DEFAULT 0,
        avg_hold_min    REAL DEFAULT 0,
        total_calc_pnl  REAL DEFAULT 0,
        carp_score      REAL DEFAULT 0,
        tier            TEXT DEFAULT 'D',
        UNIQUE(trader_address, snapshot_date)
    );

    CREATE INDEX IF NOT EXISTS idx_mainnet_trades_trader ON mainnet_trades(trader_address, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_mainnet_stats_trader ON mainnet_stats(trader_address, snapshot_date DESC);
    """)
    conn.commit()

def collect_trader(conn, alias, address, pnl_all_estimate):
    """트레이더 최신 거래내역 수집 → mainnet_trades 저장"""
    now_ms = int(time.time() * 1000)
    cur = conn.cursor()

    # 마지막 수집 시각 확인
    cur.execute(
        "SELECT MAX(created_at) FROM mainnet_trades WHERE trader_address=?",
        (address,)
    )
    row = cur.fetchone()
    last_ts = row[0] or 0

    # 거래내역 조회
    d = pm_get("/trades/history", {"address": address, "limit": 500})
    trades = d.get("data", []) or []

    # 신규 거래만 필터
    new_trades = [t for t in trades
                  if t.get("created_at", 0) > last_ts
                  and t.get("event_type") in ("fulfill_taker", "fulfill_maker", "liquidation")]

    inserted = 0
    for t in new_trades:
        tid = t.get("client_order_id") or f"{address[:8]}-{t.get('created_at')}"
        try:
            cur.execute("""
                INSERT OR IGNORE INTO mainnet_trades
                (id, trader_address, trader_alias, symbol, side, amount,
                 entry_price, event_type, cause, created_at, collected_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                tid, address, alias,
                t.get("symbol"), t.get("side"),
                float(t.get("amount", 0) or 0),
                float(t.get("entry_price", t.get("price", 0)) or 0),
                t.get("event_type"), t.get("cause"),
                t.get("created_at"), now_ms,
            ))
            inserted += 1
        except Exception as e:
            logger.debug(f"insert 스킵: {e}")

    conn.commit()

    total = cur.execute(
        "SELECT COUNT(*) FROM mainnet_trades WHERE trader_address=?", (address,)
    ).fetchone()[0]

    logger.info(f"  {alias}: 신규 {inserted}건 / 누적 {total}건 (raw trades: {len(trades)}건)")
    return total

def calc_from_mainnet_trades(conn, address) -> dict | None:
    """mainnet_trades → WR/PF/Kelly 계산"""
    cur = conn.cursor()
    cur.execute("""
        SELECT symbol, side, amount, entry_price, created_at, cause
        FROM mainnet_trades
        WHERE trader_address=? AND event_type IN ('fulfill_taker','fulfill_maker')
        ORDER BY created_at ASC
    """, (address,))
    rows = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]

    if not rows:
        return None

    positions = {}
    pnl_list  = []
    for r in rows:
        sym   = r["symbol"] or "?"
        side  = r["side"]   or ""
        amt   = float(r["amount"] or 0)
        price = float(r["entry_price"] or 0)
        ts    = r["created_at"] or 0

        if r.get("cause") == "liquidation":
            positions.pop(sym, None)
            continue

        pos = positions.get(sym)
        if pos is None:
            positions[sym] = {"side": side, "size": amt, "price": price, "ts": ts}
        elif pos["side"] != side:
            close = min(pos["size"], amt)
            pnl = (price - pos["price"]) * close if pos["side"] == "bid" else (pos["price"] - price) * close
            hold = (ts - pos["ts"]) / 1000
            pnl_list.append({"pnl": pnl, "hold": hold})
            remaining = pos["size"] - close
            if remaining > 1e-8:
                positions[sym]["size"] = remaining
            else:
                positions.pop(sym, None)
        else:
            ns  = pos["size"] + amt
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
    al   = gl / (cnt - wins) if (cnt - wins) > 0 else 1e-9
    po   = aw / al if al > 1e-9 else 9.99
    kelly = wr - (1 - wr) / po if po > 0 else -1.0
    avg_hold = sum(p["hold"] for p in pnl_list) / cnt

    return {
        "closed_cnt":   cnt,
        "win_rate":     round(wr * 100, 2),
        "profit_factor":round(pf, 4),
        "payoff_ratio": round(po, 3),
        "kelly":        round(kelly, 4),
        "avg_hold_min": round(avg_hold / 60, 1),
        "total_pnl":    round(sum(p["pnl"] for p in pnl_list), 4),
    }

def carp_score(pnl_all, stats) -> float:
    if not stats:
        return 0.0
    wr    = stats["win_rate"] / 100
    pf    = stats["profit_factor"]
    kelly = stats["kelly"]
    cnt   = stats["closed_cnt"]

    psr   = min(wr * 1.5, 1.0) if cnt >= 100 else (wr * 1.2 if cnt >= 50 else wr)
    c     = 30 * psr
    alpha = min((pf - 1) * 10, 25) if pf >= 1 and pnl_all > 0 else 0
    risk  = max(kelly * 25, 0) if kelly > 0 else 0
    pers  = min(math.log10(max(cnt, 1)) / 2.5 * 20, 20) if pnl_all > 0 else 0
    return round(min(c + alpha + risk + pers, 100), 1)

def save_snapshot(conn, alias, address, pnl_all, pnl_30d, equity, stats):
    """mainnet_stats 스냅샷 저장"""
    score = carp_score(pnl_all, stats)
    s = stats or {}
    pf = s.get("profit_factor", 0)
    k  = s.get("kelly", -1)
    cnt = s.get("closed_cnt", 0)

    tier = ("A" if score >= 55 and pf >= 2.5 and k >= 0.10 and cnt >= 100 and pnl_all >= 10_000 else
            "B" if score >= 35 and pf >= 1.5 and k >= 0.05 and cnt >= 50  and pnl_all >= 1_000 else
            "C" if k > 0 and pf >= 1.0 else "D")

    today = date.today().isoformat()
    now_ms = int(time.time() * 1000)
    conn.execute("""
        INSERT OR REPLACE INTO mainnet_stats
        (trader_address, trader_alias, snapshot_date, snapshot_ts,
         pnl_all_time, pnl_30d, equity,
         closed_cnt, win_rate, profit_factor, payoff_ratio, kelly,
         avg_hold_min, total_calc_pnl, carp_score, tier)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        address, alias, today, now_ms,
        pnl_all, pnl_30d, equity,
        s.get("closed_cnt", 0), s.get("win_rate", 0),
        s.get("profit_factor", 0), s.get("payoff_ratio", 0), s.get("kelly", 0),
        s.get("avg_hold_min", 0), s.get("total_pnl", 0),
        score, tier,
    ))
    conn.commit()
    return score, tier

def run_collection():
    """1회 수집 사이클"""
    conn = sqlite3.connect(DB_PATH)
    init_mainnet_tables(conn)
    now_ms = int(time.time() * 1000)

    # 리더보드에서 최신 수치 갱신
    lb = pm_get("/leaderboard", {"limit": 100, "orderBy": "PNL", "timePeriod": "ALL"})
    lb_map = {t.get("address"): t for t in (lb.get("data") or [])}

    results = []
    for alias, addr, _ in WATCH_TRADERS:
        lb_row = lb_map.get(addr, {})
        pnl_all = float(lb_row.get("pnl_all_time", 0) or 0)
        pnl_30  = float(lb_row.get("pnl_30d", 0) or 0)
        equity  = float(lb_row.get("equity", lb_row.get("account_equity", 0)) or 0)

        try:
            total_collected = collect_trader(conn, alias, addr, pnl_all)
            time.sleep(0.5)
        except Exception as e:
            logger.warning(f"{alias} 수집 오류: {e}")
            total_collected = 0

        # DB 내 거래로 통계 재계산
        stats = calc_from_mainnet_trades(conn, addr)
        score, tier = save_snapshot(conn, alias, addr, pnl_all, pnl_30, equity, stats)

        results.append({
            "alias": alias, "address": addr,
            "pnl_all": pnl_all, "pnl_30d": pnl_30,
            "trades_db": total_collected,
            "carp": score, "tier": tier,
            "stats": stats,
        })

    # 출력
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M KST")
    SEP = "=" * 72
    print(f"\n{SEP}")
    print(f"  Mainnet 장기 수집 | {now_str}")
    print(SEP)
    print(f"  {'트레이더':<14} {'누적거래':>8} {'PnL_all':>12} {'WR':>6} {'PF':>6} {'Kelly':>7} {'CARP':>6} {'Tier':>5}")
    print(f"  {'-'*70}")
    for r in sorted(results, key=lambda x: x["carp"], reverse=True):
        s = r["stats"] or {}
        wr_s    = f"{s.get('win_rate',0):.1f}%" if s else "N/A"
        pf_s    = f"{s.get('profit_factor',0):.2f}" if s else "N/A"
        kelly_s = f"{s.get('kelly',0):.3f}" if s else "N/A"
        cnt_s   = str(s.get("closed_cnt", 0)) if s else "0"
        star = "★" if r["tier"] == "A" else ("◆" if r["tier"] == "B" else "▶")
        print(f"  {star} {r['alias']:<13} {r['trades_db']:>8} ${r['pnl_all']:>10,.0f} "
              f"{wr_s:>6} {pf_s:>6} {kelly_s:>7} {r['carp']:>5.0f}   {r['tier']}")
    print(SEP)

    # 신뢰도 기준 현황
    tier_counts = {"A":0,"B":0,"C":0,"D":0}
    for r in results:
        tier_counts[r["tier"]] = tier_counts.get(r["tier"],0) + 1
    print(f"\n  Tier A:{tier_counts['A']} / B:{tier_counts['B']} / C:{tier_counts['C']} / D:{tier_counts['D']}")

    # 신뢰도 기준 요약 출력
    print(f"""
  ┌─ 확정 신뢰도 기준 ────────────────────────────────┐
  │  TIER A: CARP≥55, PF≥2.5, Kelly≥0.10, 거래≥100건  │
  │  TIER B: CARP≥35, PF≥1.5, Kelly≥0.05, 거래≥50건   │
  │  TIER C: Kelly>0, PF≥1.0 (데이터 누적 중)           │
  │  TIER D: Kelly≤0 또는 PF<1.0 → 복사 제외           │
  └───────────────────────────────────────────────────┘
  * 거래 데이터가 쌓일수록 WR/PF 신뢰도 상승
  * 현재 /trades/history?limit=500 제한 → 시간 누적 필요
""")

    conn.close()
    return results

def main():
    if "--loop" in sys.argv:
        logger.info(f"Mainnet Collector 루프 시작 (간격: {INTERVAL_SEC}초)")
        while True:
            try:
                run_collection()
            except Exception as e:
                logger.error(f"수집 오류: {e}")
            logger.info(f"다음 수집까지 {INTERVAL_SEC}초 대기...")
            time.sleep(INTERVAL_SEC)
    else:
        run_collection()

if __name__ == "__main__":
    main()
