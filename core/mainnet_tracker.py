"""
core/mainnet_tracker.py — 메인넷 장기 PnL 추적기
- 3시간마다 실행 → 데이터 누적 → 팔로워 PnL 자동 계산
- 트레이더 목록은 실행 시 실시간 리더보드에서 자동 선별
"""
import os, sqlite3, time, logging, json
from datetime import datetime, timezone
import requests, urllib3

urllib3.disable_warnings()
logger = logging.getLogger(__name__)

CF_URL  = "https://do5jt23sqak4.cloudfront.net"
HEADERS = {"Host": "api.pacifica.fi"}

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(_ROOT, "mainnet_tracker.db")

# 선발 기준
MIN_EQUITY   = 50_000   # 최소 자본 $50k
MIN_ROI_30D  = 10.0     # 최소 30일 ROI 10%
MIN_PNL_ALL  = 0.0      # 전체 수익 양수
N_TRADERS    = 6        # 선발 인원

SCENARIOS = [
    {"key": "conservative_1k",  "label": "Safe", "capital": 1_000,  "n": 3, "grade_filter": ["S"]},
    {"key": "balanced_5k",      "label": "Balanced", "capital": 5_000,  "n": 3, "grade_filter": ["S","A"]},
    {"key": "aggressive_10k",   "label": "Aggressive", "capital": 10_000, "n": 3, "grade_filter": ["A"]},
    {"key": "full_10k",         "label": "Full Portfolio", "capital": 10_000, "n": 6, "grade_filter": ["S","A","B"]},
]

COPY_REALISM  = 0.82
TOTAL_FEE_PCT = 0.0015

SCHEMA = """
CREATE TABLE IF NOT EXISTS trader_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at INTEGER NOT NULL, collected_date TEXT NOT NULL, collected_hour INTEGER NOT NULL,
    address TEXT NOT NULL, alias TEXT, grade TEXT, crs REAL, copy_ratio REAL DEFAULT 0.10,
    pnl_1d REAL DEFAULT 0, pnl_7d REAL DEFAULT 0, pnl_30d REAL DEFAULT 0,
    pnl_all_time REAL DEFAULT 0, equity REAL DEFAULT 0, oi REAL DEFAULT 0,
    roi_30d REAL DEFAULT 0, roi_7d REAL DEFAULT 0, roi_1d REAL DEFAULT 0,
    momentum INTEGER DEFAULT 0, live_score REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS follower_sim_pnl (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at INTEGER NOT NULL, scenario TEXT NOT NULL, label TEXT,
    capital REAL NOT NULL, n_traders INTEGER NOT NULL,
    pnl_1d REAL DEFAULT 0, pnl_7d REAL DEFAULT 0, pnl_30d REAL DEFAULT 0,
    roi_1d_pct REAL DEFAULT 0, roi_7d_pct REAL DEFAULT 0, roi_30d_pct REAL DEFAULT 0,
    win_traders INTEGER DEFAULT 0, total_traders INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS trust_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at INTEGER NOT NULL,
    traders_roi30_ge10 INTEGER DEFAULT 0, traders_roi30_ge30 INTEGER DEFAULT 0,
    avg_crs REAL DEFAULT 0, avg_roi_30d REAL DEFAULT 0,
    sim_1d_roi_1k REAL DEFAULT 0, sim_7d_roi_1k REAL DEFAULT 0,
    sim_30d_roi_1k REAL DEFAULT 0, sim_30d_roi_10k_full REAL DEFAULT 0,
    l2_met INTEGER DEFAULT 0, l3_target_met INTEGER DEFAULT 0,
    n_selected INTEGER DEFAULT 0
);
"""

def _init_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn

def mainnet_get(path, params=None):
    _verify_ssl = os.getenv("PACIFICA_VERIFY_SSL", "true").lower() in ("true", "1", "yes")
    try:
        r = requests.get(f"{CF_URL}/api/v1/{path}", headers=HEADERS, params=params, verify=_verify_ssl, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"mainnet_get({path}): {e}")
        return {}

def select_traders(n=N_TRADERS):
    """실시간 리더보드에서 조건 충족 상위 N명 자동 선별"""
    d = mainnet_get("leaderboard")
    data = d.get("data", [])
    candidates = []
    for t in data:
        eq  = float(t.get("equity_current") or 0)
        p30 = float(t.get("pnl_30d")        or 0)
        p7  = float(t.get("pnl_7d")         or 0)
        p1  = float(t.get("pnl_1d")         or 0)
        pat = float(t.get("pnl_all_time")   or 0)
        if eq < MIN_EQUITY or p30 <= 0 or pat <= MIN_PNL_ALL:
            continue
        roi30 = p30 / eq * 100
        roi7  = p7  / eq * 100
        roi1  = p1  / eq * 100
        if roi30 < MIN_ROI_30D:
            continue
        momentum = sum([1 if p1>0 else 0, 1 if p7>0 else 0, 1 if p30>0 else 0])
        score    = roi30 * momentum + roi7 * 0.5
        # 등급 부여
        if roi30 >= 40 and momentum >= 2: grade, crs, cr = "S", 82.0, 0.15
        elif roi30 >= 20 and momentum >= 2: grade, crs, cr = "A", 73.0, 0.10
        else: grade, crs, cr = "B", 60.0, 0.07
        candidates.append({
            "address": t["address"],
            "alias":   t["address"][:8],
            "grade": grade, "crs": crs, "copy_ratio": cr,
            "roi30": roi30, "roi7": roi7, "roi1": roi1,
            "pnl_30d": p30, "pnl_7d": p7, "pnl_1d": p1,
            "pnl_all_time": pat, "equity": eq,
            "oi": float(t.get("oi_current") or 0),
            "momentum": momentum, "score": score,
        })
    candidates.sort(key=lambda x: -x["score"])
    return candidates[:n]

def calc_sim_pnl(traders):
    """선발된 트레이더 → 4개 시나리오 팔로워 PnL"""
    results = []
    for sc in SCENARIOS:
        capital = sc["capital"]
        pool    = [t for t in traders if t["grade"] in sc["grade_filter"]]
        if not pool: pool = traders   # fallback
        selected = pool[:sc["n"]]
        n = len(selected)
        if n == 0:
            continue
        alloc = capital / n
        p1d = p7d = p30d = 0.0
        win = 0
        for t in selected:
            invested = alloc * t["copy_ratio"]
            ff = 1 - TOTAL_FEE_PCT
            p1d  += invested * (t["roi1"]  / 100) * COPY_REALISM * ff
            p7d  += invested * (t["roi7"]  / 100) * COPY_REALISM * ff
            p30d += invested * (t["roi30"] / 100) * COPY_REALISM * ff
            if t["roi30"] > 0: win += 1
        results.append({
            "scenario": sc["key"], "label": sc["label"],
            "capital": capital, "n_traders": n,
            "pnl_1d":  round(p1d,  4),
            "pnl_7d":  round(p7d,  4),
            "pnl_30d": round(p30d, 4),
            "roi_1d_pct":  round(p1d  / capital * 100, 4),
            "roi_7d_pct":  round(p7d  / capital * 100, 4),
            "roi_30d_pct": round(p30d / capital * 100, 4),
            "win_traders": win, "total_traders": n,
        })
    return results

def check_trust(traders, sim_results):
    rois = [t["roi30"] for t in traders]
    crss = [t["crs"]   for t in traders]
    sm   = {s["scenario"]: s for s in sim_results}
    s1k  = sm.get("conservative_1k", {})
    sfl  = sm.get("full_10k", {})
    avg_crs = sum(crss)/len(crss) if crss else 0
    return {
        "traders_roi30_ge10":   sum(1 for r in rois if r >= 10),
        "traders_roi30_ge30":   sum(1 for r in rois if r >= 30),
        "avg_crs":              round(avg_crs, 2),
        "avg_roi_30d":          round(sum(rois)/len(rois) if rois else 0, 2),
        "sim_1d_roi_1k":        s1k.get("roi_1d_pct",  0),
        "sim_7d_roi_1k":        s1k.get("roi_7d_pct",  0),
        "sim_30d_roi_1k":       s1k.get("roi_30d_pct", 0),
        "sim_30d_roi_10k_full": sfl.get("roi_30d_pct", 0),
        "l2_met":               1 if (sum(1 for r in rois if r >= 10) >= 3 and avg_crs >= 60) else 0,
        "l3_target_met":        1 if s1k.get("roi_30d_pct", 0) >= 3.0 else 0,
        "n_selected":           len(traders),
    }

def collect_once(db_path=DEFAULT_DB_PATH):
    conn = _init_db(db_path)
    now  = int(time.time())
    dt   = datetime.now(timezone.utc)
    date_str, hour = dt.strftime("%Y-%m-%d"), dt.hour

    logger.info("메인넷 실시간 트레이더 선별 중...")
    traders = select_traders()
    logger.info(f"선별 완료: {len(traders)}명")

    for t in traders:
        conn.execute("""
            INSERT INTO trader_snapshots
            (collected_at,collected_date,collected_hour,address,alias,grade,crs,copy_ratio,
             pnl_1d,pnl_7d,pnl_30d,pnl_all_time,equity,oi,roi_30d,roi_7d,roi_1d,momentum,live_score)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (now,date_str,hour, t["address"],t["alias"],t["grade"],t["crs"],t["copy_ratio"],
              t["pnl_1d"],t["pnl_7d"],t["pnl_30d"],t["pnl_all_time"],t["equity"],t["oi"],
              t["roi30"],t["roi7"],t["roi1"],t["momentum"],t["score"]))
        logger.info(f"  {t['alias']} ({t['grade']}): ROI_30d={t['roi30']:+.2f}%  ROI_7d={t['roi7']:+.2f}%  equity=${t['equity']:,.0f}")

    sim = calc_sim_pnl(traders)
    for s in sim:
        conn.execute("""
            INSERT INTO follower_sim_pnl
            (collected_at,scenario,label,capital,n_traders,
             pnl_1d,pnl_7d,pnl_30d,roi_1d_pct,roi_7d_pct,roi_30d_pct,win_traders,total_traders)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (now,s["scenario"],s["label"],s["capital"],s["n_traders"],
              s["pnl_1d"],s["pnl_7d"],s["pnl_30d"],
              s["roi_1d_pct"],s["roi_7d_pct"],s["roi_30d_pct"],
              s["win_traders"],s["total_traders"]))

    trust = check_trust(traders, sim)
    conn.execute("""
        INSERT INTO trust_metrics
        (collected_at,traders_roi30_ge10,traders_roi30_ge30,avg_crs,avg_roi_30d,
         sim_1d_roi_1k,sim_7d_roi_1k,sim_30d_roi_1k,sim_30d_roi_10k_full,
         l2_met,l3_target_met,n_selected)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (now, trust["traders_roi30_ge10"],trust["traders_roi30_ge30"],
          trust["avg_crs"],trust["avg_roi_30d"],
          trust["sim_1d_roi_1k"],trust["sim_7d_roi_1k"],
          trust["sim_30d_roi_1k"],trust["sim_30d_roi_10k_full"],
          trust["l2_met"],trust["l3_target_met"],trust["n_selected"]))

    conn.commit()
    conn.close()
    return {"collected_at":now,"collected_date":date_str,"collected":len(traders),
            "snapshots":traders,"sim_pnl":sim,"trust":trust}

def get_accumulated_report(db_path=DEFAULT_DB_PATH, days=30):
    if not os.path.exists(db_path):
        return {"error": "No data. Run collect_once() first."}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT MIN(collected_at),MAX(collected_at),COUNT(DISTINCT collected_at) FROM trader_snapshots").fetchone()
    if not r or not r[0]:
        conn.close()
        return {"error": "No data available."}
    first_ts, last_ts, n_col = r[0], r[1], r[2]
    duration_days = (last_ts - first_ts) / 86400 if last_ts > first_ts else 0

    # 최신 수집의 트레이더별 추이
    latest_ts = conn.execute("SELECT MAX(collected_at) FROM trader_snapshots").fetchone()[0]
    traders_latest = [dict(r) for r in conn.execute(
        "SELECT * FROM trader_snapshots WHERE collected_at=? ORDER BY live_score DESC", (latest_ts,)
    ).fetchall()]

    sim_latest = [dict(r) for r in conn.execute(
        "SELECT * FROM follower_sim_pnl WHERE collected_at=(SELECT MAX(collected_at) FROM follower_sim_pnl)"
    ).fetchall()]

    trust_history = [dict(r) for r in conn.execute(
        "SELECT * FROM trust_metrics ORDER BY collected_at DESC LIMIT 10"
    ).fetchall()]

    pnl_trend = [{"ts":r[0],"roi_30d_pct":r[1]} for r in conn.execute(
        "SELECT collected_at, roi_30d_pct FROM follower_sim_pnl WHERE scenario='conservative_1k' ORDER BY collected_at"
    ).fetchall()]

    conn.close()
    return {
        "meta": {
            "first_date":    datetime.fromtimestamp(first_ts).strftime("%Y-%m-%d %H:%M"),
            "latest_date":   datetime.fromtimestamp(last_ts).strftime("%Y-%m-%d %H:%M"),
            "duration_days": round(duration_days, 1),
            "n_collections": n_col,
        },
        "traders_latest":  traders_latest,
        "sim_pnl_latest":  sim_latest,
        "trust_latest":    trust_history[0] if trust_history else {},
        "trust_history":   trust_history,
        "pnl_trend":       pnl_trend,
    }
