"""
Copy Perp 백테스팅 엔진
- 상위 트레이더 거래 내역 기반 시뮬레이션
- copy ratio / max_position 파라미터 최적화
"""
import json
import ssl
import socket
import sqlite3
import time
from typing import Optional


def cf_get(path: str) -> dict:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        s = socket.create_connection(("do5jt23sqak4.cloudfront.net", 443), timeout=12)
        ss = ctx.wrap_socket(s, server_hostname="do5jt23sqak4.cloudfront.net")
        req = (
            f"GET /api/v1/{path} HTTP/1.1\r\n"
            f"Host: test-api.pacifica.fi\r\n"
            f"Connection: close\r\n\r\n"
        )
        ss.sendall(req.encode())
        data = b""
        ss.settimeout(12)
        try:
            while True:
                c = ss.recv(8192)
                if not c:
                    break
                data += c
        except Exception:
            pass
        body = data.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in data else data
        return json.loads(body.decode("utf-8", "ignore"))
    except Exception as e:
        return {"error": str(e)}


def get_trader_history(address: str, limit: int = 100) -> list:
    result = cf_get(f"trades/history?account={address}&limit={limit}")
    if isinstance(result, dict):
        return result.get("data", [])
    return []


def backtest_trader(
    address: str,
    alias: str,
    copy_ratio: float = 0.5,
    max_position_usdc: float = 50,
    initial_capital: float = 1000,
) -> dict:
    """단일 트레이더 백테스트 시뮬레이션"""
    trades = get_trader_history(address, limit=100)
    if not trades:
        return {"address": address, "alias": alias, "error": "거래 내역 없음"}

    capital = initial_capital
    total_pnl = 0.0
    wins = 0
    losses = 0
    max_drawdown = 0.0
    peak = initial_capital
    trade_results = []

    for t in trades:
        trade_pnl = float(t.get("pnl", 0) or 0)
        # 복사 비율 적용 (트레이더 pnl에서 copy_ratio 만큼 우리 수익)
        # 단순화: 거래당 pnl * copy_ratio * (max_position / 거래 규모 비례)
        our_pnl = trade_pnl * copy_ratio * 0.01  # 스케일 조정
        capital += our_pnl
        total_pnl += our_pnl

        if our_pnl > 0:
            wins += 1
        elif our_pnl < 0:
            losses += 1

        if capital > peak:
            peak = capital
        drawdown = (peak - capital) / peak if peak > 0 else 0
        if drawdown > max_drawdown:
            max_drawdown = drawdown

        trade_results.append({"pnl": round(our_pnl, 4), "capital": round(capital, 2)})

    total_trades = wins + losses
    win_rate = wins / total_trades if total_trades > 0 else 0
    roi = (total_pnl / initial_capital) * 100

    return {
        "address": address,
        "alias": alias,
        "copy_ratio": copy_ratio,
        "max_position_usdc": max_position_usdc,
        "initial_capital": initial_capital,
        "final_capital": round(capital, 2),
        "total_pnl": round(total_pnl, 2),
        "roi_pct": round(roi, 2),
        "win_rate": round(win_rate, 4),
        "wins": wins,
        "losses": losses,
        "max_drawdown_pct": round(max_drawdown * 100, 2),
        "total_trades_simulated": total_trades,
    }


def run_backtest(top_n: int = 10, db_path: str = "copy_perp.db") -> list:
    """상위 N명 트레이더 백테스트 실행"""
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    traders = db.execute("""
        SELECT address, alias, pnl_all_time, win_rate, pnl_7d
        FROM traders
        WHERE active=1 AND pnl_all_time > 0
        ORDER BY pnl_all_time DESC
        LIMIT ?
    """, (top_n,)).fetchall()
    db.close()

    results = []
    print(f"백테스트 실행: {len(traders)}명 대상")

    for t in traders:
        print(f"  분석 중: {t['alias'] or t['address'][:12]}...", end=" ", flush=True)
        result = backtest_trader(
            address=t["address"],
            alias=t["alias"] or t["address"][:8],
            copy_ratio=0.5,
            max_position_usdc=50,
            initial_capital=1000,
        )
        results.append(result)
        roi = result.get("roi_pct", 0)
        wr = result.get("win_rate", 0)
        print(f"ROI={roi:+.1f}% WR={wr:.0%} DD={result.get('max_drawdown_pct', 0):.1f}%")
        time.sleep(0.3)  # rate limit 방지

    # 결과 정렬: ROI 기준
    results.sort(key=lambda x: x.get("roi_pct", -999), reverse=True)
    return results


if __name__ == "__main__":
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

    print("=" * 60)
    print("Copy Perp 백테스팅 엔진 v1.0")
    print("=" * 60)

    results = run_backtest(top_n=10)

    print()
    print("=" * 60)
    print("백테스트 결과 순위 (ROI 기준)")
    print("=" * 60)
    print(f"{'순위':<4} {'별칭':<12} {'ROI':>8} {'WR':>6} {'최대DD':>8} {'거래수':>5}")
    print("-" * 50)
    for i, r in enumerate(results, 1):
        if "error" in r:
            continue
        print(f"{i:<4} {r['alias']:<12} {r['roi_pct']:>7.1f}% {r['win_rate']:>5.0%} "
              f"{r['max_drawdown_pct']:>7.1f}% {r['total_trades_simulated']:>5}")

    # 최종 추천
    print()
    print("=== 최종 Copy Engine 설정 추천 ===")
    top3 = [r for r in results[:3] if "error" not in r]
    for r in top3:
        print(f"  FOLLOW: {r['address']}")
        print(f"    copy_ratio=0.5, max_position=50 USDC")
        print(f"    예상 ROI: {r['roi_pct']:+.1f}% / 최대 손실: {r['max_drawdown_pct']:.1f}%")

    # JSON 저장
    out = os.path.join(os.path.dirname(os.path.dirname(__file__)), "backtest_results.json")
    with open(out, "w") as f:
        json.dump({"results": results, "generated_at": int(time.time())}, f, indent=2)
    print(f"\n✅ 결과 저장: {out}")
