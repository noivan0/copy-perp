"""
API 연결 테스트 스크립트
실제 API 키 세팅 후 바로 실행해서 검증

실행: python3 scripts/test_api_connection.py
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

from pacifica.client import PacificaClient, ACCOUNT_ADDRESS, AGENT_PRIVATE_KEY, BUILDER_CODE


def section(title: str):
    print(f"\n{'='*50}")
    print(f"  {title}")
    print('='*50)


def ok(msg): print(f"  ✅ {msg}")
def fail(msg): print(f"  ❌ {msg}")
def info(msg): print(f"  ℹ️  {msg}")


def test_env():
    section("환경변수 확인")
    checks = [
        ("ACCOUNT_ADDRESS", ACCOUNT_ADDRESS),
        ("AGENT_PRIVATE_KEY", AGENT_PRIVATE_KEY),
        ("BUILDER_CODE", BUILDER_CODE),
    ]
    all_ok = True
    for name, val in checks:
        if val:
            ok(f"{name}: {val[:12]}...")
        else:
            fail(f"{name}: 미설정")
            all_ok = False
    return all_ok


def test_markets(client: PacificaClient):
    section("마켓 데이터 조회")
    try:
        markets = client.get_markets()
        ok(f"전체 마켓: {len(markets)}개")
        btc = next((m for m in markets if m.get("symbol") == "BTC"), None)
        if btc:
            ok(f"BTC: mark={btc.get('mark_price', btc.get('mark', '?'))} "
               f"funding={btc.get('funding_rate', '?')} "
               f"OI={btc.get('open_interest', '?')}")
        return True
    except Exception as e:
        fail(f"마켓 조회 실패: {e}")
        return False


def test_account(client: PacificaClient):
    section("계정 상태 조회")
    try:
        positions = client.get_positions()
        ok(f"포지션: {len(positions)}개")
        trades = client.get_account_trades(5)
        ok(f"최근 체결: {len(trades)}건")
        for t in trades[:3]:
            info(f"  {t.get('side', '?')} {t.get('amount', '?')} "
                 f"@ {t.get('price', '?')} ({t.get('event_type', '?')})")
        return True
    except Exception as e:
        fail(f"계정 조회 실패: {e}")
        return False


def test_agent_key(client: PacificaClient):
    section("Agent Key 서명 테스트 (dry-run)")
    if not client._kp:
        fail("Agent Private Key 미설정")
        return False
    try:
        from pacifica.client import _sign_request, _sort_json_keys
        import base58
        timestamp = int(time.time() * 1000)
        header = {"timestamp": timestamp, "expiry_window": 5000, "type": "test_sign"}
        payload = {"symbol": "BTC", "side": "bid", "amount": "0.001"}
        msg, sig = _sign_request(header, payload, client._kp)
        ok(f"서명 생성 성공: {sig[:20]}...")
        ok(f"공개키: {client._kp.pubkey()}")
        return True
    except Exception as e:
        fail(f"서명 실패: {e}")
        return False


def test_market_order_dry_run(client: PacificaClient):
    section("시장가 주문 (베타 코드 등록 후 실행)")
    info("주의: 실제 자금 사용. ACCOUNT_ADDRESS 잔고 확인 필요.")
    info(f"  계정: {client.account}")
    info(f"  Builder Code: {BUILDER_CODE}")

    confirm = input("\n  실제 주문 테스트하시겠습니까? (y/N): ").strip().lower()
    if confirm != "y":
        info("스킵 — 잔고 충전 후 다시 실행하세요")
        return None

    try:
        # 최소 주문 (BTC 0.001 = ~$70)
        result = client.market_order(
            symbol="BTC",
            side="bid",
            amount="0.001",
            slippage_percent="1.0",
            builder_code=BUILDER_CODE,
        )
        if result.get("data") or result.get("success"):
            ok(f"주문 성공: {json.dumps(result, indent=2)}")
            return True
        else:
            fail(f"주문 실패: {result}")
            return False
    except Exception as e:
        fail(f"주문 예외: {e}")
        return False


async def test_ws_prices():
    section("WS 가격 스트림 (5초)")
    from pacifica.client import PriceStream
    received = []

    async def on_update(data):
        received.append(data)

    stream = PriceStream(on_update)
    try:
        await asyncio.wait_for(stream.start(), timeout=5)
    except asyncio.TimeoutError:
        pass

    if received:
        ok(f"수신: {len(received)}개 업데이트, 심볼: {len(stream.latest)}개")
        btc = stream.latest.get("BTC", {})
        if btc:
            ok(f"BTC mark={btc.get('mark')} funding={btc.get('funding')}")
    else:
        fail("WS 데이터 없음")
    return bool(received)


async def test_position_monitor():
    section("PositionMonitor (10초)")
    from core.position_monitor import PositionMonitor
    fills_received = []

    async def on_fill(event):
        fills_received.append(event)
        info(f"포지션 변화: {event}")

    monitor = PositionMonitor(ACCOUNT_ADDRESS, on_fill)
    try:
        await asyncio.wait_for(monitor.start(), timeout=10)
    except asyncio.TimeoutError:
        await monitor.stop()

    if fills_received:
        ok(f"포지션 이벤트 {len(fills_received)}건 수신")
    else:
        ok("포지션 변화 없음 (잔고 $0 또는 정상 대기 상태)")
    return True


async def main():
    print("\n🔍 Copy Perp API 연결 테스트")
    print(f"   시각: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   환경: Pacifica Testnet")

    env_ok = test_env()
    if not env_ok:
        print("\n⚠️  .env 설정 필요. .env.example 참고하세요.")
        return

    client = PacificaClient(ACCOUNT_ADDRESS)

    results = {}
    results["markets"] = test_markets(client)
    results["account"] = test_account(client)
    results["agent_key"] = test_agent_key(client)
    results["ws_prices"] = await test_ws_prices()
    results["position_monitor"] = await test_position_monitor()
    results["market_order"] = test_market_order_dry_run(client)

    section("결과 요약")
    passed = sum(1 for v in results.values() if v is True)
    skipped = sum(1 for v in results.values() if v is None)
    failed = sum(1 for v in results.values() if v is False)

    for name, result in results.items():
        icon = "✅" if result is True else ("⏭️" if result is None else "❌")
        print(f"  {icon} {name}")

    print(f"\n  통과: {passed} | 스킵: {skipped} | 실패: {failed}")
    if failed == 0:
        print("  🎉 API 연결 완료! 실제 거래 준비됨.")
    else:
        print("  ⚠️  실패 항목 확인 후 재시도하세요.")


if __name__ == "__main__":
    asyncio.run(main())
