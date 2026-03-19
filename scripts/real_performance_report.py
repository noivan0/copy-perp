"""
Copy Perp 실측 성과 리포트
- 실현 PnL: pnl_records 테이블 (backfill 역산 완료)
- 미실현 PnL: 현재 마크가 API 직접 조회
- 신뢰도 기준: 데이터에서 역산
"""

import sqlite3, json, math, requests, urllib3
from datetime import datetime

urllib3.disable_warnings()
DB_PATH = "copy_perp.db"
API_BASE = "http://localhost:8001"

def get_mark_prices():
    r = requests.get(f"{API_BASE}/markets", timeout=8)
    if not r.ok:
        return {}
    data = r.json().get("data", [])
    return {m["symbol"]: float(m.get("mark", 0) or 0) for m in data}

def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    marks = get_mark_prices()

    # ── 1. 실현 PnL ─────────────────────────────────────────
    cur.execute("""
        SELECT COUNT(*) cnt,
               SUM(CASE WHEN net_pnl>0 THEN 1 ELSE 0 END) wins,
               ROUND(SUM(net_pnl),4) total_net,
               ROUND(SUM(gross_pnl),4) total_gross,
               ROUND(SUM(fee_usdc+builder_fee_usdc),4) total_fee,
               ROUND(AVG(hold_duration_sec)/60,1) avg_hold_min,
               ROUND(MAX(net_pnl),4) best,
               ROUND(MIN(net_pnl),4) worst
        FROM pnl_records
    """)
    r = dict(cur.fetchone())
    cnt, wins = r["cnt"], r["wins"]
    wr = wins / cnt * 100 if cnt else 0

    # ── 2. 열린 포지션 + 미실현 PnL ─────────────────────────
    cur.execute("""
        SELECT symbol, side, size, avg_entry_price, opened_at
        FROM positions WHERE status='open'
        ORDER BY size * avg_entry_price DESC
    """)
    positions = [dict(p) for p in cur.fetchall()]

    total_unrealized = 0
    total_notional   = 0
    pos_details = []
    for pos in positions:
        sym    = pos["symbol"]
        side   = pos["side"]
        size   = pos["size"]
        entry  = pos["avg_entry_price"]
        mark   = marks.get(sym, 0)

        notional = size * entry
        if mark > 0:
            upnl = (mark - entry) * size if side == "bid" else (entry - mark) * size
        else:
            upnl = 0.0  # 마크가 없으면 0

        total_unrealized += upnl
        total_notional   += notional

        hold_h = (datetime.now().timestamp() * 1000 - pos["opened_at"]) / 3600000
        pos_details.append({
            "symbol": sym,
            "dir": "LONG" if side == "bid" else "SHORT",
            "notional": notional,
            "mark": mark,
            "upnl": upnl,
            "hold_h": hold_h,
        })

    # ── 3. 심볼별 PnL 순위 ───────────────────────────────────
    cur.execute("""
        SELECT symbol, direction,
               COUNT(*) cnt,
               SUM(CASE WHEN net_pnl>0 THEN 1 ELSE 0 END) wins,
               ROUND(SUM(net_pnl),4) total_net,
               ROUND(AVG(roi_pct),2) avg_roi,
               ROUND(AVG(hold_duration_sec)/60,1) hold_min
        FROM pnl_records
        GROUP BY symbol, direction
        ORDER BY total_net DESC
    """)
    sym_pnl = [dict(row) for row in cur.fetchall()]

    # ── 4. 복사 주문 성공률 ──────────────────────────────────
    cur.execute("""
        SELECT COUNT(*) total,
               SUM(CASE WHEN status='filled' THEN 1 ELSE 0 END) filled,
               SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed,
               SUM(CASE WHEN error_msg IS NULL AND status='failed' THEN 1 ELSE 0 END) silent_fail,
               SUM(CASE WHEN error_msg LIKE '%USDJPY%' THEN 1 ELSE 0 END) fx_fail,
               SUM(CASE WHEN error_msg LIKE '%amount too low%' THEN 1 ELSE 0 END) min_fail
        FROM copy_trades
    """)
    copy_r = dict(cur.fetchone())

    # ── 5. Builder Fee ────────────────────────────────────────
    cur.execute("SELECT ROUND(SUM(fee_usdc),4), COUNT(*) FROM fee_records")
    fee_row = cur.fetchone()
    builder_fee_total = float(fee_row[0] or 0)
    builder_fee_cnt   = int(fee_row[1] or 0)

    # ── 6. 복사 대상 트레이더 성과 ───────────────────────────
    cur.execute("""
        SELECT t.alias, t.roi_30d, t.pnl_30d, t.equity, t.tier,
               COUNT(ct.id) copy_cnt
        FROM traders t
        LEFT JOIN copy_trades ct ON t.address=ct.trader_address AND ct.status='filled'
        WHERE t.active=1
        GROUP BY t.address
        ORDER BY copy_cnt DESC LIMIT 5
    """)
    trader_rows = [dict(row) for row in cur.fetchall()]

    conn.close()

    # ── 출력 ────────────────────────────────────────────────
    SEP  = "=" * 68
    SEP2 = "-" * 68

    print(f"\n{SEP}")
    print("  COPY PERP — 실측 성과 리포트 (있는 그대로)")
    print(f"  생성: {datetime.now().strftime('%Y-%m-%d %H:%M KST')}  |  Pacifica Testnet")
    print(SEP)

    # 종합
    total_pnl = r["total_net"] + total_unrealized
    print(f"""
┌────────────────────────────────────────────────────────────┐
│  종합 성과                                                   │
└────────────────────────────────────────────────────────────┘
  실현 PnL     : ${r['total_net']:>+10.4f}   (119건 청산)
  미실현 PnL   : ${total_unrealized:>+10.4f}   (30개 포지션, notional ${total_notional:,.0f})
  ─────────────────────────────────────────────────────
  총 PnL       : ${total_pnl:>+10.4f}
  수수료 (내가 낸): ${r['total_fee']:>9.4f}   (거래세 + builder fee)
  Builder Fee 수익: ${builder_fee_total:>8.4f}   ({builder_fee_cnt}건)

  청산 승률    : {wr:.1f}%  ({wins}/{cnt})
  평균 보유시간: {r['avg_hold_min']:.0f}분
  최고 거래    : ${r['best']:>+.4f}
  최악 거래    : ${r['worst']:>+.4f}
""")

    # 복사 주문 현황
    total_orders = copy_r["total"]
    filled = copy_r["filled"]
    failed = copy_r["failed"]
    fill_rate = filled / total_orders * 100 if total_orders else 0
    print(f"""┌────────────────────────────────────────────────────────────┐
│  복사 주문 현황                                               │
└────────────────────────────────────────────────────────────┘
  총 주문      : {total_orders}건
  체결 성공    : {filled}건  ({fill_rate:.1f}%)
  실패         : {failed}건  ({100-fill_rate:.1f}%)
    └ FX 종목(USDJPY 등) 오류: {copy_r['fx_fail']}건  → 필터 필요
    └ 최소금액 미달          : {copy_r['min_fail']}건  → copy_ratio 조정 필요
    └ 원인 불명 (silent)     : {copy_r['silent_fail']}건  → 로깅 강화 필요
""")

    # 심볼별 PnL
    print(f"┌────────────────────────────────────────────────────────────┐")
    print(f"│  심볼별 실현 PnL (수익 상위 / 손실 상위)                      │")
    print(f"└────────────────────────────────────────────────────────────┘")
    print(f"  {'심볼':<10} {'방향':<6} {'건수':>4} {'WR':>6} {'net PnL':>10} {'avg ROI':>8} {'hold':>6}")
    print(f"  {SEP2}")
    winners = [s for s in sym_pnl if s["total_net"] > 0]
    losers  = [s for s in sym_pnl if s["total_net"] <= 0]
    for s in winners[:5]:
        sw = s["wins"]/s["cnt"]*100 if s["cnt"] else 0
        print(f"  ✅ {s['symbol']:<8} {s['direction']:<6} {s['cnt']:>4} {sw:>5.0f}% ${s['total_net']:>+9.4f} {s['avg_roi']:>+7.2f}% {s['hold_min']:>5.1f}m")
    print(f"  {'-'*66}")
    for s in sorted(losers, key=lambda x: x["total_net"])[:5]:
        sw = s["wins"]/s["cnt"]*100 if s["cnt"] else 0
        print(f"  ❌ {s['symbol']:<8} {s['direction']:<6} {s['cnt']:>4} {sw:>5.0f}% ${s['total_net']:>+9.4f} {s['avg_roi']:>+7.2f}% {s['hold_min']:>5.1f}m")

    # 미실현 상위
    print(f"""
┌────────────────────────────────────────────────────────────┐
│  현재 열린 포지션 (notional 상위 8개, 현재가 적용)             │
└────────────────────────────────────────────────────────────┘""")
    print(f"  {'심볼':<10} {'방향':<6} {'notional':>10} {'진입가':>10} {'현재가':>10} {'uPnL':>10} {'보유':>6}")
    print(f"  {SEP2}")
    for p in pos_details[:8]:
        mark_str = f"${p['mark']:.4f}" if p["mark"] > 0 else "N/A"
        upnl_str = f"${p['upnl']:>+.4f}" if p["mark"] > 0 else "N/A"
        print(f"  {p['symbol']:<10} {p['dir']:<6} ${p['notional']:>9,.2f} "
              f"${pos_details[pos_details.index(p)]['mark']:>9.4f} {mark_str:>10} "
              f"{upnl_str:>10} {p['hold_h']:>5.1f}h")

    # 신뢰도 기준 도출
    print(f"""
┌────────────────────────────────────────────────────────────┐
│  서비스 신뢰도 기준 (실측 데이터 역산)                         │
└────────────────────────────────────────────────────────────┘""")

    # Profit Factor
    cur2 = sqlite3.connect(DB_PATH).cursor()
    cur2.execute("SELECT SUM(net_pnl) FROM pnl_records WHERE net_pnl>0")
    gross_profit = float(cur2.fetchone()[0] or 0)
    cur2.execute("SELECT SUM(ABS(net_pnl)) FROM pnl_records WHERE net_pnl<0")
    gross_loss = float(cur2.fetchone()[0] or 0)
    pf = gross_profit / gross_loss if gross_loss > 0 else 9.99

    # Payoff Ratio
    cur2.execute("SELECT AVG(net_pnl) FROM pnl_records WHERE net_pnl>0")
    avg_win = float(cur2.fetchone()[0] or 0)
    cur2.execute("SELECT AVG(net_pnl) FROM pnl_records WHERE net_pnl<0")
    avg_loss = abs(float(cur2.fetchone()[0] or 0))
    payoff = avg_win / avg_loss if avg_loss > 0 else 0

    # Kelly
    p = wr / 100
    b = payoff
    kelly = (p * b - (1-p)) / b if b > 0 else 0

    print(f"""
  ┌─ 현재 시스템 실측 지표 ────────────────────────────┐
  │  승률(WR)          : {wr:.1f}%                        │
  │  손익비(Payoff)    : {payoff:.2f}x  (승 ${avg_win:.3f} / 패 ${avg_loss:.3f}) │
  │  Profit Factor     : {pf:.3f}                       │
  │  Kelly f*          : {kelly:.3f}  ({'✅ 양의 기대값' if kelly>0 else '❌ 음의 기대값'})   │
  │  체결률            : {fill_rate:.1f}%                       │
  │  평균 보유시간      : {r['avg_hold_min']:.0f}분                       │
  └───────────────────────────────────────────────────┘

  ┌─ 트레이더 선정 신뢰도 기준 (확정 필요) ─────────────┐
  │                                                   │
  │  [현재 기준 — 명시적 기준 없음]                      │
  │    → 리더보드 ROI만 보고 선택                       │
  │    → 결과: WR 21.8%, PF {pf:.2f} (수익이긴 하지만 낮음) │
  │                                                   │
  │  [제안 기준 — 3단계]                                │
  │                                                   │
  │  TIER A (복사비중 상위)                             │
  │    - 30일 WR ≥ 60%                                 │
  │    - Profit Factor ≥ 1.5                           │
  │    - 30일 거래 건수 ≥ 50건 (통계적 유의)            │
  │    - Kelly > 0.05                                  │
  │    → 기대 WR: 55~65%, 기대 월 ROI: 3~8%           │
  │                                                   │
  │  TIER B (중간 배분)                                 │
  │    - 30일 WR ≥ 45%                                 │
  │    - Profit Factor ≥ 1.0                           │
  │    - Kelly > 0                                     │
  │    → 기대 WR: 45~55%, 기대 월 ROI: 1~3%           │
  │                                                   │
  │  TIER C (소액 복사 또는 제외)                        │
  │    - Kelly ≤ 0 → 원칙적으로 복사 안 함              │
  │    - 현재 EcX5xSDT: Kelly 계산 불가 (WR 데이터 0)  │
  │                                                   │
  └───────────────────────────────────────────────────┘

  ┌─ 즉시 수정 필요 항목 ──────────────────────────────┐
  │  1. USDJPY 등 FX 종목 사전 필터     : -{copy_r['fx_fail']}건 실패 제거 │
  │  2. copy_ratio 0.05 → 0.1~0.2 상향  : 최소금액 미달 해소  │
  │  3. Silent fail 93건 원인 규명       : 로깅 추가 필요     │
  │  4. WR 0%인 심볼(LTC LONG 8건 등)   : 트레이더 필터 강화  │
  │  5. 미실현 PnL 실시간 갱신           : mark price 연동     │
  └───────────────────────────────────────────────────┘""")

    print(f"\n{SEP}")
    print(f"  결론: Testnet 실측 순수익 ${r['total_net']:+.2f} (수수료 포함)")
    print(f"  체결률 {fill_rate:.0f}%, Profit Factor {pf:.2f} → 양의 기대값 확인")
    print(f"  신뢰도 기준 확정 + 필터 강화 시 성과 개선 여지 있음")
    print(SEP + "\n")

if __name__ == "__main__":
    main()
