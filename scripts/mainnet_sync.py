#!/usr/bin/env python3
"""
메인넷 트레이더 데이터 수집 + CRS 계산 + DB 저장
실행: python3 scripts/mainnet_sync.py [--init] [--loop N]
  --init: 최초 1회 전체 수집 (8252명)
  --loop N: N분마다 반복 수집 (기본 30분)
"""

import sys
import os
import json
import time
import asyncio
import urllib.parse
import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path

# 프로젝트 루트를 Python 경로에 추가
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── 설정 ────────────────────────────────────────────────────────────────
DB_PATH = str(PROJECT_ROOT / "copy_perp.db")
PROXY = "https://api.codetabs.com/v1/proxy/?quest="
LEADERBOARD_URL = "https://api.pacifica.fi/api/v1/leaderboard"
REPORTS_DIR = PROJECT_ROOT / "reports"
REPORTS_DIR.mkdir(exist_ok=True)


# ── 데이터 수집 ─────────────────────────────────────────────────────────
def fetch_mainnet_leaderboard(limit: int = 25000) -> list:
    """codetabs 프록시를 통해 메인넷 리더보드 수집"""
    try:
        from scrapling import Fetcher
    except ImportError:
        logger.error("scrapling 미설치. pip install scrapling 실행 후 재시도.")
        sys.exit(1)

    f = Fetcher(verify=False)
    target = f"{LEADERBOARD_URL}?limit={limit}"
    proxy_url = PROXY + urllib.parse.quote(target)
    logger.info(f"리더보드 수집 중... (limit={limit})")
    try:
        page = f.get(proxy_url, timeout=60)
        raw_text = page.get_all_text()
        data = json.loads(raw_text)
        traders = data.get("data", [])
        logger.info(f"✅ 트레이더 {len(traders)}명 수집 완료")
        return traders
    except json.JSONDecodeError as e:
        logger.error(f"JSON 파싱 실패: {e}")
        logger.debug(f"응답 내용(앞 500자): {raw_text[:500]}")
        return []
    except Exception as e:
        logger.error(f"리더보드 수집 실패: {e}")
        return []


# ── CRS 점수 계산 ────────────────────────────────────────────────────────
def calc_crs(t: dict) -> dict:
    """
    메인넷 트레이더 CRS 계산
    입력: leaderboard API 응답 dict
    출력: {crs, grade, momentum, profitability, risk, consistency, copyability,
           recommended_copy_ratio, roi_30d, warnings}
    """
    pnl30   = float(t.get("pnl_30d", 0) or 0)
    pnl7    = float(t.get("pnl_7d", 0) or 0)
    pnl1    = float(t.get("pnl_1d", 0) or 0)
    pnl_all = float(t.get("pnl_all_time", 0) or 0)
    eq      = float(t.get("equity_current", 0) or 0)
    vol30   = float(t.get("volume_30d", 0) or 0)

    # 원금 추정 = equity_current - pnl_all_time
    initial_capital = max(eq - pnl_all, 1)
    roi_30d = pnl30 / initial_capital * 100

    # 일관성 (0~4): 각 기간 수익 플러스 여부
    consistency = sum([pnl1 > 0, pnl7 > 0, pnl30 > 0, pnl_all > 0])

    # 모멘텀 (0~100): 최근 수익 가속도
    momentum = min(100, max(0, (pnl7 / pnl30 * 100) if pnl30 > 0 else 0))

    # 수익성 (0~100): ROI 기반 (200% ROI = 100점)
    profitability = min(100, max(0, roi_30d / 2))

    # 리스크 (0~100): OI/Equity 레버리지
    oi = float(t.get("oi_current", 0) or 0)
    leverage = oi / eq if eq > 0 else 10
    risk_score = max(0, 100 - leverage * 10)

    # 일관성 점수 (0~100)
    consistency_score = consistency / 4 * 100

    # 복사가능성 (0~100): 거래량 기반 ($1M vol = 100점)
    copyability = min(100, vol30 / 10000)

    # CRS 가중합
    crs = (
        momentum      * 0.30 +
        profitability * 0.25 +
        risk_score    * 0.20 +
        consistency_score * 0.15 +
        copyability   * 0.10
    )

    grade = (
        "S" if crs >= 85 else
        "A" if crs >= 65 else
        "B" if crs >= 50 else
        "C"
    )

    warnings = []
    if pnl30 > 0 and pnl7 / pnl30 > 0.9:
        warnings.append("단발성 의심 (7일에 30일 수익 90% 집중)")
    if leverage > 5:
        warnings.append(f"고레버리지 위험 ({leverage:.1f}x)")

    recommended_copy_ratio = (
        0.15 if grade == "S" else
        0.10 if grade == "A" else
        0.07
    )

    return {
        "crs":                  round(crs, 1),
        "grade":                grade,
        "momentum":             round(momentum, 1),
        "profitability":        round(profitability, 1),
        "risk":                 round(risk_score, 1),
        "consistency":          round(consistency_score, 1),
        "copyability":          round(copyability, 1),
        "recommended_copy_ratio": recommended_copy_ratio,
        "roi_30d":              round(roi_30d, 2),
        "leverage":             round(leverage, 2),
        "warnings":             warnings,
    }


# ── DB 초기화 (equity_daily 테이블 포함) ────────────────────────────────
EQUITY_DAILY_DDL = """
CREATE TABLE IF NOT EXISTS equity_daily (
    address         TEXT NOT NULL,
    date            TEXT NOT NULL,
    equity          REAL DEFAULT 0,
    pnl_1d          REAL DEFAULT 0,
    pnl_7d          REAL DEFAULT 0,
    pnl_30d         REAL DEFAULT 0,
    oi_current      REAL DEFAULT 0,
    synced_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (address, date)
);
"""

async def ensure_equity_daily_table(conn) -> None:
    """equity_daily 테이블이 없으면 생성"""
    await conn.executescript(EQUITY_DAILY_DDL)
    await conn.commit()


async def ensure_traders_columns(conn) -> None:
    """traders 테이블에 누락 컬럼 추가 (마이그레이션)"""
    extra_cols = [
        "ALTER TABLE traders ADD COLUMN pnl_1d REAL DEFAULT 0",
        "ALTER TABLE traders ADD COLUMN pnl_7d REAL DEFAULT 0",
        "ALTER TABLE traders ADD COLUMN pnl_30d REAL DEFAULT 0",
        "ALTER TABLE traders ADD COLUMN pnl_all_time REAL DEFAULT 0",
        "ALTER TABLE traders ADD COLUMN equity REAL DEFAULT 0",
        "ALTER TABLE traders ADD COLUMN oi REAL DEFAULT 0",
        "ALTER TABLE traders ADD COLUMN volume_7d REAL DEFAULT 0",
        "ALTER TABLE traders ADD COLUMN volume_30d REAL DEFAULT 0",
        "ALTER TABLE traders ADD COLUMN oi_current REAL DEFAULT 0",
        "ALTER TABLE traders ADD COLUMN roi_30d REAL DEFAULT 0",
        "ALTER TABLE traders ADD COLUMN crs_score REAL DEFAULT 0",
        "ALTER TABLE traders ADD COLUMN crs_grade TEXT DEFAULT 'C'",
        "ALTER TABLE traders ADD COLUMN equity_current REAL DEFAULT 0",
        "ALTER TABLE traders ADD COLUMN rank_num INTEGER DEFAULT 0",
    ]
    for sql in extra_cols:
        try:
            await conn.execute(sql)
        except Exception:
            pass
    await conn.commit()


# ── 트레이더 DB 저장 ─────────────────────────────────────────────────────
async def save_traders(conn, traders: list, crs_map: dict) -> int:
    """traders 테이블에 INSERT OR REPLACE"""
    saved = 0
    now_ts = int(time.time() * 1000)
    for t in traders:
        addr = t.get("address", "")
        if not addr:
            continue
        crs = crs_map.get(addr, {})
        try:
            await conn.execute("""
                INSERT OR REPLACE INTO traders
                    (address, alias, active, created_at, last_synced,
                     pnl_1d, pnl_7d, pnl_30d, pnl_all_time,
                     equity, equity_current, oi, oi_current,
                     volume_7d, volume_30d, roi_30d,
                     crs_score, crs_grade, tier)
                VALUES (?,?,1,COALESCE((SELECT created_at FROM traders WHERE address=?),?),?,
                        ?,?,?,?,
                        ?,?,?,?,
                        ?,?,?,
                        ?,?,?)
            """, (
                addr,
                t.get("alias") or addr[:8],
                addr,
                now_ts,
                now_ts,
                float(t.get("pnl_1d", 0) or 0),
                float(t.get("pnl_7d", 0) or 0),
                float(t.get("pnl_30d", 0) or 0),
                float(t.get("pnl_all_time", 0) or 0),
                float(t.get("equity_current", 0) or 0),
                float(t.get("equity_current", 0) or 0),
                float(t.get("oi_current", 0) or 0),
                float(t.get("oi_current", 0) or 0),
                float(t.get("volume_7d", 0) or 0),
                float(t.get("volume_30d", 0) or 0),
                crs.get("roi_30d", 0),
                crs.get("crs", 0),
                crs.get("grade", "C"),
                crs.get("grade", "C"),
            ))
            saved += 1
        except Exception as e:
            logger.debug(f"traders 저장 실패 ({addr}): {e}")
    await conn.commit()
    return saved


# ── equity_daily 스냅샷 저장 ─────────────────────────────────────────────
async def save_daily_snapshots(conn, traders: list) -> int:
    """오늘 날짜 기준 equity_daily 스냅샷 저장"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    saved = 0
    for t in traders:
        addr = t.get("address", "")
        if not addr:
            continue
        try:
            await conn.execute("""
                INSERT OR REPLACE INTO equity_daily
                    (address, date, equity, pnl_1d, pnl_7d, pnl_30d, oi_current)
                VALUES (?,?,?,?,?,?,?)
            """, (
                addr,
                today,
                float(t.get("equity_current", 0) or 0),
                float(t.get("pnl_1d", 0) or 0),
                float(t.get("pnl_7d", 0) or 0),
                float(t.get("pnl_30d", 0) or 0),
                float(t.get("oi_current", 0) or 0),
            ))
            saved += 1
        except Exception as e:
            logger.debug(f"equity_daily 저장 실패 ({addr}): {e}")
    await conn.commit()
    return saved


# ── 전일 대비 변화 조회 ───────────────────────────────────────────────────
async def get_prev_day_equity(conn, address: str) -> dict | None:
    """equity_daily에서 오늘 바로 이전 날짜 행 조회"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        async with conn.execute("""
            SELECT * FROM equity_daily
            WHERE address = ? AND date < ?
            ORDER BY date DESC LIMIT 1
        """, (address, today)) as cur:
            row = await cur.fetchone()
        if row:
            return dict(row)
    except Exception:
        pass
    return None


# ── 콘솔 출력 ────────────────────────────────────────────────────────────
def print_crs_report(traders: list, crs_map: dict, today: str) -> None:
    """메인넷 CRS 분석 결과 콘솔 출력"""
    grade_counts = {"S": [], "A": [], "B": [], "C": []}
    for t in traders:
        addr = t.get("address", "")
        crs = crs_map.get(addr)
        if crs:
            grade_counts[crs["grade"]].append((t, crs))

    total = len(traders)
    print(f"\n{'='*60}")
    print(f"=== 메인넷 CRS 분석 ({today}) ===")
    print(f"{'='*60}")
    print(f"총 트레이더: {total:,}명")
    print(
        f"S등급: {len(grade_counts['S'])}명 | "
        f"A등급: {len(grade_counts['A'])}명 | "
        f"B등급: {len(grade_counts['B'])}명 | "
        f"C등급: {len(grade_counts['C'])}명"
    )
    print()

    for grade in ["S", "A"]:
        items = sorted(grade_counts[grade], key=lambda x: x[1]["crs"], reverse=True)
        for t, crs in items:
            addr = t.get("address", "")[:8]
            pnl30 = float(t.get("pnl_30d", 0) or 0)
            vol30 = float(t.get("volume_30d", 0) or 0)
            consistency_raw = sum([
                float(t.get("pnl_1d", 0) or 0) > 0,
                float(t.get("pnl_7d", 0) or 0) > 0,
                float(t.get("pnl_30d", 0) or 0) > 0,
                float(t.get("pnl_all_time", 0) or 0) > 0,
            ])
            print(f"[{grade}등급] {addr} | CRS {crs['crs']} | "
                  f"ROI {crs['roi_30d']:.1f}% | "
                  f"PnL30 ${pnl30:,.0f} | "
                  f"추천 copy_ratio {crs['recommended_copy_ratio']*100:.0f}%")
            print(f"  강점: PnL30d ${pnl30:,.0f} | "
                  f"Vol30 ${vol30/1e6:.1f}M | "
                  f"일관성 {consistency_raw}/4")
            if crs["warnings"]:
                for w in crs["warnings"]:
                    print(f"  ⚠️  경고: {w}")
            print()

    # B등급 상위 5개만 요약
    b_items = sorted(grade_counts["B"], key=lambda x: x[1]["crs"], reverse=True)[:5]
    if b_items:
        print(f"[B등급 상위 5명]")
        for t, crs in b_items:
            addr = t.get("address", "")[:8]
            pnl30 = float(t.get("pnl_30d", 0) or 0)
            print(f"  {addr} | CRS {crs['crs']} | PnL30 ${pnl30:,.0f}")
        print()


# ── 마크다운 리포트 저장 ──────────────────────────────────────────────────
async def save_md_report(
    conn,
    traders: list,
    crs_map: dict,
    today: str,
) -> Path:
    """reports/mainnet-crs-YYYYMMDD.md 저장"""
    grade_counts: dict[str, list] = {"S": [], "A": [], "B": [], "C": []}
    for t in traders:
        addr = t.get("address", "")
        crs = crs_map.get(addr)
        if crs:
            grade_counts[crs["grade"]].append((t, crs))

    total = len(traders)
    date_tag = today.replace("-", "")
    report_path = REPORTS_DIR / f"mainnet-crs-{date_tag}.md"

    lines = [
        f"# 메인넷 CRS 분석 리포트 — {today}",
        "",
        "## 개요",
        f"- **수집일시:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"- **총 트레이더:** {total:,}명",
        f"- **S등급:** {len(grade_counts['S'])}명 | "
        f"**A등급:** {len(grade_counts['A'])}명 | "
        f"**B등급:** {len(grade_counts['B'])}명 | "
        f"**C등급:** {len(grade_counts['C'])}명",
        "",
    ]

    # S/A 등급 상세
    for grade in ["S", "A"]:
        items = sorted(grade_counts[grade], key=lambda x: x[1]["crs"], reverse=True)
        if not items:
            continue
        lines.append(f"## {grade}등급 트레이더")
        lines.append("")
        for t, crs in items:
            addr = t.get("address", "")
            pnl30 = float(t.get("pnl_30d", 0) or 0)
            pnl7  = float(t.get("pnl_7d", 0) or 0)
            pnl1  = float(t.get("pnl_1d", 0) or 0)
            pnl_all = float(t.get("pnl_all_time", 0) or 0)
            vol30 = float(t.get("volume_30d", 0) or 0)
            eq    = float(t.get("equity_current", 0) or 0)
            oi    = float(t.get("oi_current", 0) or 0)
            consistency_raw = sum([pnl1>0, pnl7>0, pnl30>0, pnl_all>0])

            # 팔로워 예상 PnL (copy_ratio 기준 10% 자금 복사 가정, 팔로워 자본 $1000)
            follower_capital = 1000
            follower_pnl_est = pnl30 * crs["recommended_copy_ratio"] * 0.1  # 매우 보수적

            lines += [
                f"### {addr[:8]} (`{addr}`)",
                "",
                f"| 항목 | 값 |",
                f"|------|-----|",
                f"| **CRS 점수** | {crs['crs']} ({grade}등급) |",
                f"| **ROI 30d** | {crs['roi_30d']:.1f}% |",
                f"| **모멘텀** | {crs['momentum']:.1f} |",
                f"| **수익성** | {crs['profitability']:.1f} |",
                f"| **리스크** | {crs['risk']:.1f} (레버리지 {crs['leverage']:.1f}x) |",
                f"| **일관성** | {crs['consistency']:.1f} ({consistency_raw}/4) |",
                f"| **복사가능성** | {crs['copyability']:.1f} |",
                f"| **추천 copy_ratio** | {crs['recommended_copy_ratio']*100:.0f}% |",
                f"| PnL 1d | ${pnl1:,.2f} |",
                f"| PnL 7d | ${pnl7:,.2f} |",
                f"| PnL 30d | ${pnl30:,.2f} |",
                f"| PnL All-time | ${pnl_all:,.2f} |",
                f"| Volume 30d | ${vol30:,.0f} |",
                f"| Equity | ${eq:,.2f} |",
                f"| OI Current | ${oi:,.2f} |",
                f"| 팔로워 예상 PnL (보수적, $1k 자본) | ~${follower_pnl_est:,.1f} |",
                "",
            ]

            # 전일 대비
            prev = await get_prev_day_equity(conn, addr)
            if prev:
                prev_eq = prev.get("equity", 0)
                eq_chg = eq - prev_eq
                lines.append(
                    f"**전일 대비:** 자산 "
                    f"{'▲' if eq_chg >= 0 else '▼'}"
                    f"${abs(eq_chg):,.2f} ({prev.get('date', '?')} → {today})"
                )
                lines.append("")

            if crs["warnings"]:
                lines.append("**⚠️ 경고:**")
                for w in crs["warnings"]:
                    lines.append(f"- {w}")
                lines.append("")

    # B등급 요약 테이블
    b_items = sorted(grade_counts["B"], key=lambda x: x[1]["crs"], reverse=True)
    if b_items:
        lines += [
            "## B등급 트레이더 (상위 20명)",
            "",
            "| 주소 | CRS | ROI 30d | PnL 30d | 일관성 | copy_ratio |",
            "|------|-----|---------|---------|--------|-----------|",
        ]
        for t, crs in b_items[:20]:
            addr = t.get("address", "")
            pnl30 = float(t.get("pnl_30d", 0) or 0)
            consistency_raw = sum([
                float(t.get("pnl_1d", 0) or 0) > 0,
                float(t.get("pnl_7d", 0) or 0) > 0,
                float(t.get("pnl_30d", 0) or 0) > 0,
                float(t.get("pnl_all_time", 0) or 0) > 0,
            ])
            lines.append(
                f"| {addr[:8]} | {crs['crs']} | {crs['roi_30d']:.1f}% | "
                f"${pnl30:,.0f} | {consistency_raw}/4 | {crs['recommended_copy_ratio']*100:.0f}% |"
            )
        lines.append("")

    # 등급별 통계
    lines += [
        "## 등급별 통계",
        "",
        "| 등급 | 인원 | 비율 |",
        "|------|------|------|",
    ]
    for grade in ["S", "A", "B", "C"]:
        cnt = len(grade_counts[grade])
        pct = cnt / total * 100 if total > 0 else 0
        lines.append(f"| {grade} | {cnt:,}명 | {pct:.1f}% |")

    lines += [
        "",
        "---",
        f"*자동 생성: mainnet_sync.py — {datetime.now(timezone.utc).isoformat()}*",
    ]

    report_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"📄 리포트 저장: {report_path}")
    return report_path


# ── 메인 로직 ────────────────────────────────────────────────────────────
async def run_sync(is_init: bool = False) -> None:
    """1회 동기화 실행"""
    import aiosqlite

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    logger.info(f"=== 메인넷 동기화 시작 ({today}) ===")

    # DB 연결
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row

    # 기존 init_db 사용 (database.py)
    try:
        from db.database import init_db
        conn = await init_db(DB_PATH)
    except Exception as e:
        logger.warning(f"init_db 로드 실패, 기본 연결 사용: {e}")
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")

    # 추가 테이블/컬럼 보장
    await ensure_equity_daily_table(conn)
    await ensure_traders_columns(conn)

    # 리더보드 수집
    limit = 25000 if is_init else 5000
    traders = fetch_mainnet_leaderboard(limit=limit)
    if not traders:
        logger.error("트레이더 데이터 없음. 종료.")
        await conn.close()
        return

    # CRS 계산
    logger.info("CRS 계산 중...")
    crs_map: dict[str, dict] = {}
    for t in traders:
        addr = t.get("address", "")
        if addr:
            crs_map[addr] = calc_crs(t)

    # DB 저장
    logger.info("DB 저장 중...")
    saved_traders = await save_traders(conn, traders, crs_map)
    saved_snapshots = await save_daily_snapshots(conn, traders)
    logger.info(f"✅ traders: {saved_traders}개, equity_daily: {saved_snapshots}개 저장")

    # 콘솔 출력
    print_crs_report(traders, crs_map, today)

    # 마크다운 리포트 저장
    report_path = await save_md_report(conn, traders, crs_map, today)
    print(f"\n📄 리포트 저장됨: {report_path}")

    # equity_daily 확인
    async with conn.execute(
        "SELECT COUNT(*) as cnt, MAX(date) as latest FROM equity_daily WHERE date = ?",
        (today,)
    ) as cur:
        row = await cur.fetchone()
        if row:
            print(f"📊 equity_daily 오늘 스냅샷: {row['cnt']:,}건 ({row['latest']})")

    await conn.close()
    logger.info("=== 동기화 완료 ===")


def run_loop(interval_min: int = 30) -> None:
    """N분마다 동기화 반복"""
    logger.info(f"🔄 루프 모드 시작 (간격: {interval_min}분)")
    first = True
    while True:
        try:
            asyncio.run(run_sync(is_init=first))
        except KeyboardInterrupt:
            logger.info("중단 신호 수신. 종료.")
            break
        except Exception as e:
            logger.error(f"동기화 실패: {e}", exc_info=True)
        first = False
        logger.info(f"⏱ {interval_min}분 후 재수집...")
        time.sleep(interval_min * 60)


# ── 진입점 ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="메인넷 트레이더 CRS 수집 파이프라인")
    parser.add_argument("--init", action="store_true", help="최초 전체 수집 (25000명)")
    parser.add_argument("--loop", type=int, default=0, metavar="N",
                        help="N분마다 반복 수집 (0=1회만)")
    args = parser.parse_args()

    if args.loop > 0:
        run_loop(args.loop)
    else:
        asyncio.run(run_sync(is_init=args.init))
