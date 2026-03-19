"""
core/mainnet_tracker.py — 메인넷 장기 PnL 추적기

매 실행 시:
1. Pacifica 메인넷 리더보드에서 CRS 상위 트레이더 실적 수집
2. mainnet_tracker.db에 시계열로 저장 (실행할수록 데이터 쌓임)
3. 팔로워 시뮬 PnL 자동 계산 (4개 시나리오)
4. 신뢰도 기준 달성 여부 자동 체크

API 접근: CloudFront SNI (HMG 방화벽 우회)
  CF_URL = https://do5jt23sqak4.cloudfront.net
  Host: api.pacifica.fi
"""

import os
import json
import math
import sqlite3
import time
import logging
from datetime import datetime, timezone

import requests
import urllib3

urllib3.disable_warnings()
logger = logging.getLogger(__name__)

# ── 접속 설정 ──────────────────────────────────────────────────────
CF_URL   = "https://do5jt23sqak4.cloudfront.net"
HEADERS  = {"Host": "api.pacifica.fi"}

_ROOT       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(_ROOT, "mainnet_tracker.db")

# ── 추적 대상 트레이더 (CRS 기준 선별) ────────────────────────────
TARGET_TRADERS = [
    {"address": "7gV81bz99MUBVb2aLYxW7MG1RMDdRdJYTPyC2syjba8y", "alias": "7gV81bz9", "crs": 86.1, "grade": "S", "copy_ratio": 0.15},
    {"address": "E1vabqxiuUfB29BAwLppTLLNMAq6HJqp7gSz1NiYwWz7", "alias": "E1vabqxi", "crs": 85.6, "grade": "S", "copy_ratio": 0.15},
    {"address": "5BPd5WYVvDE2kHMjzGmLHMaAorSm8bEfERcsycg5GCAD", "alias": "5BPd5WYV", "crs": 80.9, "grade": "S", "copy_ratio": 0.15},
    {"address": "EYhhf8u9M6kN9tCRVgd2Jki9fJm3XzJRnTF9k5eBC1q1", "alias": "EYhhf8u9", "crs": 78.3, "grade": "A", "copy_ratio": 0.10},
    {"address": "4UBH19qUbXEaqyz9fKrFHuvj8BPMoM87H71s1YPKyGYq",  "alias": "4UBH19qU", "crs": 75.2, "grade": "A", "copy_ratio": 0.10},
    {"address": "A6VY4ZBUohgSLkwMuDwDvAnzgiXFB1eTDzaixyitPJep",  "alias": "A6VY4ZBU", "crs": 72.3, "grade": "A", "copy_ratio": 0.10},
]

# ── 팔로워 시나리오 정의 ──────────────────────────────────────────
SCENARIOS = [
    {"key": "conservative_1k",  "label": "안정형",  "capital": 1_000,  "traders": ["7gV81bz9", "E1vabqxi", "5BPd5WYV"]},
    {"key": "balanced_5k",      "label": "균형형",  "capital": 5_000,  "traders": ["7gV81bz9", "EYhhf8u9", "4UBH19qU"]},
    {"key": "aggressive_10k",   "label": "공격형",  "capital": 10_000, "traders": ["4UBH19qU", "A6VY4ZBU", "EYhhf8u9"]},
    {"key": "full_10k",         "label": "풀포트",  "capital": 10_000, "traders": ["7gV81bz9", "E1vabqxi", "5BPd5WYV", "4UBH19qU", "A6VY4ZBU"]},
]

# ── 현실화 계수 ───────────────────────────────────────────────────
COPY_REALISM   = 0.82    # 슬리피지 + 지연 + 부분체결 손실 반영
TOTAL_FEE_PCT  = 0.0015  # taker(0.05%) + builder(0.10%) = 0.15% per trade


# ── DB 초기화 ─────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS trader_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at    INTEGER NOT NULL,
    collected_date  TEXT NOT NULL,
    collected_hour  INTEGER NOT NULL,
    address         TEXT NOT NULL,
    alias           TEXT,
    grade           TEXT,
    crs             REAL,
    pnl_1d          REAL DEFAULT 0,
    pnl_7d          REAL DEFAULT 0,
    pnl_30d         REAL DEFAULT 0,
    pnl_all_time    REAL DEFAULT 0,
    equity          REAL DEFAULT 0,
    oi              REAL DEFAULT 0,
    roi_30d         REAL DEFAULT 0,
    roi_7d          REAL DEFAULT 0,
    roi_1d          REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS follower_sim_pnl (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at    INTEGER NOT NULL,
    scenario        TEXT NOT NULL,
    label           TEXT,
    capital         REAL NOT NULL,
    n_traders       INTEGER NOT NULL,
    pnl_1d          REAL DEFAULT 0,
    pnl_7d          REAL DEFAULT 0,
    pnl_30d         REAL DEFAULT 0,
    roi_1d_pct      REAL DEFAULT 0,
    roi_7d_pct      REAL DEFAULT 0,
    roi_30d_pct     REAL DEFAULT 0,
    win_traders     INTEGER DEFAULT 0,
    total_traders   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS trust_metrics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at    INTEGER NOT NULL,
    traders_roi30_ge10   INTEGER DEFAULT 0,
    traders_roi30_ge30   INTEGER DEFAULT 0,
    avg_crs              REAL DEFAULT 0,
    avg_roi_30d          REAL DEFAULT 0,
    sim_1d_roi_1k        REAL DEFAULT 0,
    sim_7d_roi_1k        REAL DEFAULT 0,
    sim_30d_roi_1k       REAL DEFAULT 0,
    sim_30d_roi_10k_full REAL DEFAULT 0,
    l2_met               INTEGER DEFAULT 0,
    l3_target_met        INTEGER DEFAULT 0
);
"""


def _init_db(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True) if os.path.dirname(db_path) else None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ── API 조회 ──────────────────────────────────────────────────────
def mainnet_get(path: str, params: dict = None) -> dict:
    url = f"{CF_URL}/api/v1/{path}"
    try:
        r = requests.get(url, headers=HEADERS, params=params, verify=False, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"mainnet_get({path}) 오류: {e}")
        return {}


def fetch_trader(address: str) -> dict:
    """트레이더 1명의 최신 실적 조회"""
    d = mainnet_get("leaderboard", {"account": address})
    data = d.get("data", [])
    if isinstance(data, list) and data:
        return data[0]
    # 리더보드 전체에서 검색
    full = mainnet_get("leaderboard", {"limit": 500})
    for t in full.get("data", []):
        if t.get("address") == address:
            return t
    return {}


# ── 팔로워 시뮬 PnL 계산 ─────────────────────────────────────────
def calc_follower_sim_pnl(snapshots: list) -> list:
    """
    트레이더 스냅샷 리스트 → 4개 시나리오 팔로워 PnL 계산
    수익 현실화 계수(0.82) + 수수료(0.15%) 적용
    """
    snap_by_alias = {s["alias"]: s for s in snapshots}
    results = []

    for sc in SCENARIOS:
        capital  = sc["capital"]
        aliases  = sc["traders"]
        n        = len(aliases)
        alloc    = capital / n   # 트레이더당 균등 배분

        pnl_1d = pnl_7d = pnl_30d = 0.0
        win_cnt = 0

        for alias in aliases:
            snap = snap_by_alias.get(alias)
            if not snap:
                continue
            t = next((x for x in TARGET_TRADERS if x["alias"] == alias), None)
            if not t:
                continue

            cr = t["copy_ratio"]
            invested = alloc * cr  # 실제 투자 금액

            # 트레이더 ROI → 팔로워 PnL (현실화 + 수수료)
            r1d  = snap.get("roi_1d",  0) / 100
            r7d  = snap.get("roi_7d",  0) / 100
            r30d = snap.get("roi_30d", 0) / 100

            fee_factor = (1 - TOTAL_FEE_PCT)
            p1d  = invested * r1d  * COPY_REALISM * fee_factor
            p7d  = invested * r7d  * COPY_REALISM * fee_factor
            p30d = invested * r30d * COPY_REALISM * fee_factor

            pnl_1d  += p1d
            pnl_7d  += p7d
            pnl_30d += p30d

            if snap.get("roi_30d", 0) > 0:
                win_cnt += 1

        results.append({
            "scenario":     sc["key"],
            "label":        sc["label"],
            "capital":      capital,
            "n_traders":    n,
            "pnl_1d":       round(pnl_1d,  4),
            "pnl_7d":       round(pnl_7d,  4),
            "pnl_30d":      round(pnl_30d, 4),
            "roi_1d_pct":   round(pnl_1d  / capital * 100, 4),
            "roi_7d_pct":   round(pnl_7d  / capital * 100, 4),
            "roi_30d_pct":  round(pnl_30d / capital * 100, 4),
            "win_traders":  win_cnt,
            "total_traders": n,
        })

    return results


# ── 신뢰도 체크 ───────────────────────────────────────────────────
def check_trust_metrics(snapshots: list, sim_results: list) -> dict:
    rois = [s.get("roi_30d", 0) for s in snapshots]
    crss = [s.get("crs", 0) for s in snapshots]

    traders_ge10 = sum(1 for r in rois if r >= 10)
    traders_ge30 = sum(1 for r in rois if r >= 30)
    avg_crs      = sum(crss) / len(crss) if crss else 0
    avg_roi_30d  = sum(rois) / len(rois) if rois else 0

    sim_map = {s["scenario"]: s for s in sim_results}
    sim_1k   = sim_map.get("conservative_1k", {})
    sim_full = sim_map.get("full_10k", {})

    l2_met = 1 if (traders_ge10 >= 3 and avg_crs >= 70) else 0
    l3_met = 1 if sim_1k.get("roi_30d_pct", 0) >= 5.0 else 0

    return {
        "traders_roi30_ge10":   traders_ge10,
        "traders_roi30_ge30":   traders_ge30,
        "avg_crs":              round(avg_crs, 2),
        "avg_roi_30d":          round(avg_roi_30d, 2),
        "sim_1d_roi_1k":        sim_1k.get("roi_1d_pct", 0),
        "sim_7d_roi_1k":        sim_1k.get("roi_7d_pct", 0),
        "sim_30d_roi_1k":       sim_1k.get("roi_30d_pct", 0),
        "sim_30d_roi_10k_full": sim_full.get("roi_30d_pct", 0),
        "l2_met":               l2_met,
        "l3_target_met":        l3_met,
    }


# ── 1회 수집 ─────────────────────────────────────────────────────
def collect_once(db_path: str = DEFAULT_DB_PATH) -> dict:
    """
    메인넷에서 트레이더 실적 수집 → DB 저장 → 시뮬 계산 → 신뢰도 체크
    반환: {"collected": n, "snapshots": [...], "sim_pnl": [...], "trust": {...}}
    """
    conn = _init_db(db_path)
    now  = int(time.time())
    dt   = datetime.now(timezone.utc)
    date_str = dt.strftime("%Y-%m-%d")
    hour     = dt.hour

    snapshots = []

    # 리더보드 전체 1회 조회 (API 절약)
    logger.info("메인넷 리더보드 조회 중...")
    full_lb = mainnet_get("leaderboard", {"limit": 1000})
    lb_map  = {t["address"]: t for t in full_lb.get("data", [])}

    for target in TARGET_TRADERS:
        addr  = target["address"]
        alias = target["alias"]

        raw = lb_map.get(addr) or fetch_trader(addr)
        if not raw:
            logger.warning(f"  {alias}: 데이터 없음 — 스킵")
            continue

        def _f(key): return float(raw.get(key) or 0)

        equity  = _f("equity_current")
        pnl_30d = _f("pnl_30d")
        pnl_7d  = _f("pnl_7d")
        pnl_1d  = _f("pnl_1d")

        roi_30d = round(pnl_30d / equity * 100, 4) if equity > 0 else 0
        roi_7d  = round(pnl_7d  / equity * 100, 4) if equity > 0 else 0
        roi_1d  = round(pnl_1d  / equity * 100, 4) if equity > 0 else 0

        snap = {
            "collected_at":   now,
            "collected_date": date_str,
            "collected_hour": hour,
            "address":  addr,
            "alias":    alias,
            "grade":    target["grade"],
            "crs":      target["crs"],
            "pnl_1d":   pnl_1d,
            "pnl_7d":   pnl_7d,
            "pnl_30d":  pnl_30d,
            "pnl_all_time": _f("pnl_all_time"),
            "equity":   equity,
            "oi":       _f("oi_current"),
            "roi_30d":  roi_30d,
            "roi_7d":   roi_7d,
            "roi_1d":   roi_1d,
        }
        snapshots.append(snap)

        conn.execute("""
            INSERT INTO trader_snapshots
            (collected_at, collected_date, collected_hour,
             address, alias, grade, crs,
             pnl_1d, pnl_7d, pnl_30d, pnl_all_time,
             equity, oi, roi_30d, roi_7d, roi_1d)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            snap["collected_at"], snap["collected_date"], snap["collected_hour"],
            snap["address"], snap["alias"], snap["grade"], snap["crs"],
            snap["pnl_1d"], snap["pnl_7d"], snap["pnl_30d"], snap["pnl_all_time"],
            snap["equity"], snap["oi"], snap["roi_30d"], snap["roi_7d"], snap["roi_1d"],
        ))
        logger.info(f"  {alias} ({target['grade']}): ROI_30d={roi_30d:+.2f}% PnL_30d=${pnl_30d:,.0f}")

    # 팔로워 시뮬 PnL
    sim_results = calc_follower_sim_pnl(snapshots)
    for sim in sim_results:
        conn.execute("""
            INSERT INTO follower_sim_pnl
            (collected_at, scenario, label, capital, n_traders,
             pnl_1d, pnl_7d, pnl_30d, roi_1d_pct, roi_7d_pct, roi_30d_pct,
             win_traders, total_traders)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            now, sim["scenario"], sim["label"], sim["capital"], sim["n_traders"],
            sim["pnl_1d"], sim["pnl_7d"], sim["pnl_30d"],
            sim["roi_1d_pct"], sim["roi_7d_pct"], sim["roi_30d_pct"],
            sim["win_traders"], sim["total_traders"],
        ))

    # 신뢰도 체크
    trust = check_trust_metrics(snapshots, sim_results)
    conn.execute("""
        INSERT INTO trust_metrics
        (collected_at, traders_roi30_ge10, traders_roi30_ge30, avg_crs, avg_roi_30d,
         sim_1d_roi_1k, sim_7d_roi_1k, sim_30d_roi_1k, sim_30d_roi_10k_full,
         l2_met, l3_target_met)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        now,
        trust["traders_roi30_ge10"], trust["traders_roi30_ge30"],
        trust["avg_crs"], trust["avg_roi_30d"],
        trust["sim_1d_roi_1k"], trust["sim_7d_roi_1k"],
        trust["sim_30d_roi_1k"], trust["sim_30d_roi_10k_full"],
        trust["l2_met"], trust["l3_target_met"],
    ))

    conn.commit()
    conn.close()

    return {
        "collected_at": now,
        "collected_date": date_str,
        "collected": len(snapshots),
        "snapshots": snapshots,
        "sim_pnl":   sim_results,
        "trust":     trust,
    }


# ── 누적 보고서 ───────────────────────────────────────────────────
def get_accumulated_report(db_path: str = DEFAULT_DB_PATH, days: int = 30) -> dict:
    """
    누적 데이터 기반 장기 보고서
    - 트레이더별 ROI 추이 (첫 수집 vs 최신)
    - 팔로워 시나리오별 최신 PnL
    - 신뢰도 기준 달성 이력
    """
    if not os.path.exists(db_path):
        return {"error": "아직 수집된 데이터가 없습니다. collect_once() 먼저 실행하세요."}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # 수집 횟수 + 기간
    cur = conn.execute("SELECT MIN(collected_at), MAX(collected_at), COUNT(DISTINCT collected_at) FROM trader_snapshots")
    r = cur.fetchone()
    if not r or not r[0]:
        conn.close()
        return {"error": "데이터 없음"}

    first_ts, last_ts, n_collections = r[0], r[1], r[2]
    duration_days = (last_ts - first_ts) / 86400 if last_ts > first_ts else 0

    # 트레이더별 첫 수집 vs 최신
    trader_trend = []
    for t in TARGET_TRADERS:
        addr = t["address"]
        cur = conn.execute(
            "SELECT roi_30d, pnl_30d, equity, collected_date FROM trader_snapshots WHERE address=? ORDER BY collected_at ASC LIMIT 1",
            (addr,)
        )
        first = cur.fetchone()
        cur = conn.execute(
            "SELECT roi_30d, pnl_30d, equity, collected_date FROM trader_snapshots WHERE address=? ORDER BY collected_at DESC LIMIT 1",
            (addr,)
        )
        latest = cur.fetchone()

        if first and latest:
            trader_trend.append({
                "alias":      t["alias"],
                "grade":      t["grade"],
                "crs":        t["crs"],
                "roi_30d_first":  float(first["roi_30d"]),
                "roi_30d_latest": float(latest["roi_30d"]),
                "roi_30d_delta":  round(float(latest["roi_30d"]) - float(first["roi_30d"]), 2),
                "pnl_30d_latest": float(latest["pnl_30d"]),
                "equity_latest":  float(latest["equity"]),
                "first_date":     first["collected_date"],
                "latest_date":    latest["collected_date"],
            })

    # 최신 시뮬 PnL
    cur = conn.execute(
        "SELECT * FROM follower_sim_pnl WHERE collected_at=(SELECT MAX(collected_at) FROM follower_sim_pnl)"
    )
    sim_latest = [dict(r) for r in cur.fetchall()]

    # 신뢰도 기준 달성 이력 (최근 10회)
    cur = conn.execute(
        "SELECT * FROM trust_metrics ORDER BY collected_at DESC LIMIT 10"
    )
    trust_history = [dict(r) for r in cur.fetchall()]
    trust_latest  = trust_history[0] if trust_history else {}

    # 데이터 포인트별 누적 추이 (시나리오: conservative_1k)
    cur = conn.execute(
        "SELECT collected_at, collected_date, roi_30d_pct FROM follower_sim_pnl WHERE scenario='conservative_1k' ORDER BY collected_at ASC"
    )
    pnl_trend = [{"ts": r[0], "date": r[1], "roi_30d_pct": r[2]} for r in cur.fetchall()]

    conn.close()

    return {
        "meta": {
            "first_date":    datetime.fromtimestamp(first_ts).strftime("%Y-%m-%d %H:%M"),
            "latest_date":   datetime.fromtimestamp(last_ts).strftime("%Y-%m-%d %H:%M"),
            "duration_days": round(duration_days, 1),
            "n_collections": n_collections,
            "n_traders":     len(trader_trend),
        },
        "trader_trend":    trader_trend,
        "sim_pnl_latest":  sim_latest,
        "trust_latest":    trust_latest,
        "trust_history":   trust_history,
        "pnl_trend":       pnl_trend,
    }
