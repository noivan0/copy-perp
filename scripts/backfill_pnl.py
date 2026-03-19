"""
기존 copy_trades → pnl_records / positions / equity_snapshots 역산 채우기
- bid/ask 순서로 시간순 재처리
- open → close 페어링하여 실현 PnL 계산
- 기존 pnl 컬럼($0.05 수준)은 덮어씌움
"""

import asyncio
import sqlite3
import uuid
import time
import math
from datetime import datetime

DB_PATH = "copy_perp.db"
BUILDER_FEE_RATE = 0.001   # 0.1%
TRADE_FEE_RATE   = 0.0005  # 0.05%


def _calc_pnl(side, entry, exit_p, size):
    if side == "bid":
        return (exit_p - entry) * size
    return (entry - exit_p) * size


def backfill():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 체결된 거래 시간순
    cur.execute("""
        SELECT id, follower_address, trader_address, symbol, side, amount, exec_price, created_at
        FROM copy_trades
        WHERE status='filled' AND exec_price IS NOT NULL AND exec_price > 0
        ORDER BY created_at ASC
    """)
    trades = [dict(r) for r in cur.fetchall()]

    print(f"총 {len(trades)}건 체결 거래 역산 처리...")

    # 팔로워별 심볼별 포지션 상태
    # {follower: {symbol: {side, size, avg_price, opened_at, open_trade_id}}}
    positions = {}
    pnl_records = []
    equity_snapshots = {}  # {follower: [(ts, cum_pnl)]}
    cum_pnl = {}  # {follower: float}

    for t in trades:
        follower = t["follower_address"]
        symbol   = t["symbol"]
        side     = t["side"]
        size     = float(t["amount"])
        price    = float(t["exec_price"])
        ts       = t["created_at"]
        trade_id = t["id"]
        trader   = t["trader_address"]

        if follower not in positions:
            positions[follower] = {}
        if follower not in cum_pnl:
            cum_pnl[follower] = 0.0
        if follower not in equity_snapshots:
            equity_snapshots[follower] = []

        pos = positions[follower].get(symbol)

        if pos is None:
            # 신규 포지션
            positions[follower][symbol] = {
                "side": side, "size": size,
                "avg_price": price,
                "opened_at": ts, "open_trade_id": trade_id,
                "trader": trader,
            }
            pos_action = "open"
            realized_pnl = None

        elif pos["side"] == side:
            # 같은 방향 — 추가매수 (평균 진입가 갱신)
            old_size  = pos["size"]
            old_price = pos["avg_price"]
            new_size  = old_size + size
            new_price = (old_price * old_size + price * size) / new_size
            positions[follower][symbol]["size"]      = new_size
            positions[follower][symbol]["avg_price"] = new_price
            pos_action = "add"
            realized_pnl = None

        else:
            # 반대 방향 — 청산
            entry_price = pos["avg_price"]
            close_size  = min(pos["size"], size)
            gross = _calc_pnl(pos["side"], entry_price, price, close_size)
            fee   = close_size * price * TRADE_FEE_RATE
            b_fee = close_size * price * BUILDER_FEE_RATE
            net   = gross - fee - b_fee
            cost  = entry_price * close_size
            roi   = (net / cost * 100) if cost > 0 else 0
            hold  = int((ts - pos["opened_at"]) / 1000)

            pnl_records.append({
                "id":               str(uuid.uuid4()),
                "follower_address": follower,
                "trader_address":   trader,
                "symbol":           symbol,
                "direction":        "long" if pos["side"] == "bid" else "short",
                "open_trade_id":    pos["open_trade_id"],
                "close_trade_id":   trade_id,
                "size":             close_size,
                "entry_price":      entry_price,
                "exit_price":       price,
                "gross_pnl":        gross,
                "fee_usdc":         fee,
                "builder_fee_usdc": b_fee,
                "net_pnl":          net,
                "roi_pct":          roi,
                "hold_duration_sec":hold,
                "opened_at":        pos["opened_at"],
                "closed_at":        ts,
                "created_at":       int(time.time() * 1000),
            })

            cum_pnl[follower] += net
            equity_snapshots[follower].append((ts, cum_pnl[follower]))
            realized_pnl = net
            pos_action = "close"

            # 잔여 수량 처리
            remaining = pos["size"] - close_size
            if remaining > 1e-8:
                positions[follower][symbol]["size"] = remaining
            else:
                del positions[follower][symbol]

                # 초과 수량은 반대 방향 신규 포지션
                flip = size - close_size
                if flip > 1e-8:
                    positions[follower][symbol] = {
                        "side": side, "size": flip,
                        "avg_price": price,
                        "opened_at": ts, "open_trade_id": trade_id,
                        "trader": trader,
                    }
                    pos_action = "flip"

        # copy_trades 업데이트
        dt_str = datetime.fromtimestamp(ts / 1000).isoformat() if ts else None
        cur.execute("""
            UPDATE copy_trades SET
                realized_pnl=?, position_action=?, created_at_dt=?
            WHERE id=?
        """, (realized_pnl, pos_action, dt_str, trade_id))

    # pnl_records 삽입 (중복 스킵)
    inserted_pnl = 0
    for r in pnl_records:
        try:
            cur.execute("""
                INSERT OR IGNORE INTO pnl_records
                (id, follower_address, trader_address, symbol, direction,
                 open_trade_id, close_trade_id, size, entry_price, exit_price,
                 gross_pnl, fee_usdc, builder_fee_usdc, net_pnl, roi_pct,
                 hold_duration_sec, opened_at, closed_at, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (r["id"], r["follower_address"], r["trader_address"], r["symbol"],
                  r["direction"], r["open_trade_id"], r["close_trade_id"],
                  r["size"], r["entry_price"], r["exit_price"],
                  r["gross_pnl"], r["fee_usdc"], r["builder_fee_usdc"],
                  r["net_pnl"], r["roi_pct"], r["hold_duration_sec"],
                  r["opened_at"], r["closed_at"], r["created_at"]))
            inserted_pnl += 1
        except Exception as e:
            print(f"  pnl_records 스킵: {e}")

    # 열린 포지션 DB 반영
    inserted_pos = 0
    for follower, syms in positions.items():
        for symbol, pos in syms.items():
            pos_id = str(uuid.uuid4())
            now_ms = int(time.time() * 1000)
            try:
                cur.execute("""
                    INSERT OR IGNORE INTO positions
                    (id, follower_address, trader_address, symbol, side, size,
                     avg_entry_price, initial_size, initial_entry_price,
                     open_trade_id, mark_price, opened_at, last_updated, status)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (pos_id, follower, pos["trader"], symbol, pos["side"],
                      pos["size"], pos["avg_price"],
                      pos["size"], pos["avg_price"],
                      pos["open_trade_id"], pos["avg_price"],
                      pos["opened_at"], now_ms, "open"))
                inserted_pos += 1
            except Exception as e:
                print(f"  positions 스킵: {e}")

    # equity_snapshots 삽입
    inserted_snap = 0
    for follower, snaps in equity_snapshots.items():
        base_equity = 10_000.0  # 기본 초기 자본 가정
        for ts, cum in snaps:
            bucket = (ts // (15 * 60 * 1000)) * (15 * 60 * 1000)
            try:
                cur.execute("""
                    INSERT OR IGNORE INTO equity_snapshots
                    (follower_address, equity_usdc, realized_pnl_cum, snapshot_at)
                    VALUES (?,?,?,?)
                """, (follower, base_equity + cum, cum, bucket))
                inserted_snap += 1
            except Exception as e:
                pass  # 같은 bucket 중복은 무시

    conn.commit()

    # 결과 출력
    print(f"\n=== Backfill 완료 ===")
    print(f"pnl_records 삽입  : {inserted_pnl}건")
    print(f"positions 삽입    : {inserted_pos}개 (현재 열린 포지션)")
    print(f"equity_snapshots  : {inserted_snap}건")

    # 팔로워별 성과 요약
    print(f"\n=== 팔로워별 실현 PnL ===")
    cur.execute("""
        SELECT follower_address,
               COUNT(*) cnt,
               SUM(CASE WHEN net_pnl>0 THEN 1 ELSE 0 END) wins,
               ROUND(SUM(net_pnl),4) total_net,
               ROUND(AVG(roi_pct),2) avg_roi,
               ROUND(SUM(fee_usdc+builder_fee_usdc),4) total_fee
        FROM pnl_records
        GROUP BY follower_address
        ORDER BY total_net DESC
    """)
    for r in cur.fetchall():
        addr = r[0][:16] + "..."
        wr = round(r[2]/r[1]*100, 1) if r[1] else 0
        print(f"  {addr}: {r[1]}건 WR={wr}% net={r[3]:+.4f} avg_roi={r[4]:+.2f}% fee=${r[5]:.4f}")

    # 심볼별 PnL 분포
    print(f"\n=== 심볼별 실현 PnL TOP 10 ===")
    cur.execute("""
        SELECT symbol, direction, COUNT(*) cnt,
               ROUND(SUM(net_pnl),4) total_net,
               ROUND(AVG(roi_pct),2) avg_roi,
               ROUND(AVG(hold_duration_sec)/60,1) avg_hold_min
        FROM pnl_records
        GROUP BY symbol, direction
        ORDER BY total_net DESC
        LIMIT 10
    """)
    for r in cur.fetchall():
        sign = "+" if r[3] >= 0 else ""
        print(f"  {r[0]:<8} {r[1]:<6}: {r[2]}건 net={sign}{r[3]:.4f} roi={r[4]:+.2f}% hold={r[5]}분")

    conn.close()


if __name__ == "__main__":
    backfill()
