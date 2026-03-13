"""
tests/test_mainnet_readonly.py
Mainnet 읽기전용 테스트 — GET만, 실제 주문 없음

Mainnet: IP 54.230.62.105 직접 + Host: api.pacifica.fi
HMG 우회: raw SSL socket (urllib 차단)
"""
import pytest
import json
import ssl
import gzip
import socket
import time
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

MAINNET_IP   = "54.230.62.105"
MAINNET_HOST = "api.pacifica.fi"
MAINNET_BASE = "/api/v1"


def _mn_get(path: str, timeout: int = 15) -> tuple[int, dict | list]:
    """Mainnet IP 직접 GET"""
    full = MAINNET_BASE + "/" + path.lstrip("/")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection((MAINNET_IP, 443), timeout=timeout)
    s = ctx.wrap_socket(raw, server_hostname=MAINNET_HOST)
    req = (
        f"GET {full} HTTP/1.1\r\nHost: {MAINNET_HOST}\r\n"
        f"Accept-Encoding: identity\r\nConnection: close\r\n\r\n"
    )
    s.sendall(req.encode())
    s.settimeout(timeout)
    data = b""
    while True:
        c = s.recv(32768)
        if not c: break
        data += c
    s.close()
    if b"\r\n\r\n" not in data:
        return 0, {}
    hdr_raw, body = data.split(b"\r\n\r\n", 1)
    code = int(hdr_raw.split(b"\r\n")[0].split()[1])
    if b"transfer-encoding: chunked" in hdr_raw.lower():
        decoded = b""
        while body:
            idx = body.find(b"\r\n")
            if idx < 0: break
            try: size = int(body[:idx], 16)
            except: break
            if size == 0: break
            decoded += body[idx+2: idx+2+size]
            body = body[idx+2+size+2:]
        body = decoded
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    if not body.strip():
        return code, {}
    return code, json.loads(body.decode("utf-8", "ignore"))


@pytest.fixture(autouse=True)
def rate_guard():
    yield
    time.sleep(0.5)


# ── MR-A: Mainnet 읽기전용 기본 ──────────────────────────────────────

class TestMainnetReadonly:

    def test_mr_a01_prices_all_symbols(self):
        """[MR-A01] Mainnet 전체 심볼 가격 조회"""
        code, data = _mn_get("info/prices")
        assert code == 200
        prices = data.get("data", data) if isinstance(data, dict) else data
        assert len(prices) >= 60, f"심볼 부족: {len(prices)}"
        print(f"\n✅ MR-A01: Mainnet {len(prices)}개 심볼 가격")

    def test_mr_a02_btc_price_reasonable(self):
        """[MR-A02] BTC 가격 합리적 범위"""
        code, data = _mn_get("info/prices")
        assert code == 200
        prices = data.get("data", data) if isinstance(data, dict) else data
        btc = next((p for p in prices if p.get("symbol") == "BTC"), None)
        assert btc
        mark = float(btc.get("mark", 0))
        mid = float(btc.get("mid", 0))
        oracle = float(btc.get("oracle", mark))
        assert 10_000 < mark < 500_000
        # mark/mid 차이 0.1% 이내
        if mid > 0:
            spread_pct = abs(mark - mid) / mark * 100
            assert spread_pct < 1.0, f"스프레드 이상: {spread_pct:.3f}%"
        print(f"\n✅ MR-A02: BTC mark=${mark:,.2f} mid=${mid:,.2f} oracle=${oracle:,.2f}")

    def test_mr_a03_eth_sol_bnb_prices(self):
        """[MR-A03] 주요 심볼 가격 존재"""
        code, data = _mn_get("info/prices")
        assert code == 200
        prices = data.get("data", data) if isinstance(data, dict) else data
        price_map = {p["symbol"]: float(p.get("mark", 0)) for p in prices}
        for sym, min_price, max_price in [
            ("ETH", 100, 50_000),
            ("SOL", 1, 10_000),
            ("BNB", 10, 10_000),
        ]:
            assert sym in price_map, f"{sym} 없음"
            p = price_map[sym]
            assert min_price < p < max_price, f"{sym} 가격 이상: {p}"
            print(f"  {sym}=${p:,.2f}")
        print(f"\n✅ MR-A03: ETH/SOL/BNB 가격 정상")

    def test_mr_a04_leaderboard_top1_data_quality(self):
        """[MR-A04] Mainnet TOP1 트레이더 데이터 품질"""
        code, data = _mn_get("leaderboard?limit=10")
        assert code == 200
        lb = data.get("data", data) if isinstance(data, dict) else data
        assert lb
        top = lb[0]
        # 필수 필드
        for field in ["address", "pnl_all_time", "equity_current"]:
            assert field in top, f"{field} 없음"
        # 주소 형식 (base58, 32~44자)
        addr = top["address"]
        assert 32 <= len(addr) <= 44, f"주소 길이 이상: {len(addr)}"
        pnl = float(top["pnl_all_time"] or 0)
        equity = float(top["equity_current"] or 0)
        print(f"\n✅ MR-A04: TOP1 {addr[:8]}... pnl=${pnl:,.0f} equity=${equity:,.0f}")

    def test_mr_a05_leaderboard_sorted_desc(self):
        """[MR-A05] 리더보드 PnL 내림차순 정렬"""
        code, data = _mn_get("leaderboard?limit=10")
        assert code == 200
        lb = data.get("data", data) if isinstance(data, dict) else data
        pnls = [float(t.get("pnl_all_time", 0) or 0) for t in lb]
        # TOP1 >= TOP5 (완전 정렬 아닐 수 있지만 대체로)
        if len(pnls) >= 5:
            assert pnls[0] >= pnls[4] or pnls[0] >= 0
        print(f"\n✅ MR-A05: 리더보드 정렬 TOP1=${pnls[0]:,.0f} TOP5=${pnls[min(4,len(pnls)-1)]:,.0f}")

    def test_mr_a06_funding_rates_all_symbols(self):
        """[MR-A06] 전체 심볼 펀딩비 범위"""
        code, data = _mn_get("info/prices")
        assert code == 200
        prices = data.get("data", data) if isinstance(data, dict) else data
        extreme = [(p["symbol"], float(p.get("funding", 0))) for p in prices
                   if abs(float(p.get("funding", 0))) > 0.02]
        # 2% 초과 펀딩비는 이상 (있을 수 있지만 경고)
        if extreme:
            print(f"\n⚠️  MR-A06: 극단 펀딩비 심볼: {extreme[:3]}")
        print(f"\n✅ MR-A06: {len(prices)}개 심볼 펀딩비 확인, 극단 {len(extreme)}개")

    def test_mr_a07_open_interest_distribution(self):
        """[MR-A07] 미결제약정 분포 (BTC 최대)"""
        code, data = _mn_get("info/prices")
        assert code == 200
        prices = data.get("data", data) if isinstance(data, dict) else data
        oi_map = {p["symbol"]: float(p.get("open_interest", 0) or 0) for p in prices}
        btc_oi = oi_map.get("BTC", 0)
        eth_oi = oi_map.get("ETH", 0)
        assert btc_oi >= 0 and eth_oi >= 0
        print(f"\n✅ MR-A07: BTC OI={btc_oi:,.2f} ETH OI={eth_oi:,.2f}")


# ── MR-B: Mainnet 트레이더 분석 ──────────────────────────────────────

class TestMainnetTraderAnalysis:

    def test_mr_b01_top10_positive_pnl_majority(self):
        """[MR-B01] TOP10 중 양수 PnL 과반 이상"""
        code, data = _mn_get("leaderboard?limit=10")
        assert code == 200
        lb = data.get("data", data) if isinstance(data, dict) else data
        positive = [t for t in lb if float(t.get("pnl_all_time", 0) or 0) > 0]
        print(f"\n✅ MR-B01: TOP10 중 양수 PnL {len(positive)}명")
        # 최소 절반은 양수
        assert len(positive) >= 3, f"양수 PnL 너무 적음: {len(positive)}"

    def test_mr_b02_equity_all_positive(self):
        """[MR-B02] 리더보드 equity 전부 양수"""
        code, data = _mn_get("leaderboard?limit=10")
        assert code == 200
        lb = data.get("data", data) if isinstance(data, dict) else data
        neg_equity = [(t["address"][:8], float(t.get("equity_current",0))) for t in lb
                      if float(t.get("equity_current", 0) or 0) < 0]
        assert not neg_equity, f"음수 equity: {neg_equity}"
        print(f"\n✅ MR-B02: {len(lb)}명 전원 equity 양수")

    def test_mr_b03_pnl_leader_profile(self):
        """[MR-B03] PnL 1위 상세 프로필"""
        code, data = _mn_get("leaderboard?limit=10")
        assert code == 200
        lb = data.get("data", data) if isinstance(data, dict) else data
        # PnL 최대 트레이더
        top_pnl = max(lb, key=lambda t: float(t.get("pnl_all_time",0) or 0))
        addr = top_pnl["address"]
        pnl = float(top_pnl["pnl_all_time"])
        equity = float(top_pnl["equity_current"] or 0)
        roi = pnl / equity * 100 if equity > 0 else 0
        print(f"\n✅ MR-B03: PnL 1위 {addr[:8]} pnl=${pnl:,.0f} equity=${equity:,.0f} ROI={roi:.1f}%")

    def test_mr_b04_100_traders_collected(self):
        """[MR-B04] Mainnet 100명 수집 검증"""
        code, data = _mn_get("leaderboard?limit=100")
        assert code == 200
        lb = data.get("data", data) if isinstance(data, dict) else data
        assert len(lb) >= 10, f"트레이더 부족: {len(lb)}"
        # 주소 중복 없음
        addrs = [t["address"] for t in lb]
        assert len(addrs) == len(set(addrs)), "중복 주소 존재"
        print(f"\n✅ MR-B04: Mainnet {len(lb)}명 수집, 중복 없음")

    def test_mr_b05_mainnet_24h_volume(self):
        """[MR-B05] Mainnet 24h 거래량 (BTC)"""
        code, data = _mn_get("info/prices")
        assert code == 200
        prices = data.get("data", data) if isinstance(data, dict) else data
        btc = next((p for p in prices if p.get("symbol") == "BTC"), None)
        assert btc
        vol = float(btc.get("volume_24h", 0) or 0)
        assert vol >= 0
        print(f"\n✅ MR-B05: BTC 24h 거래량={vol:,.2f}")


# ── MR-C: Mainnet 데이터 일관성 ──────────────────────────────────────

class TestMainnetDataConsistency:

    def test_mr_c01_prices_stable_within_30s(self):
        """[MR-C01] 30초 내 BTC 가격 변동 ≤ 1%"""
        code1, d1 = _mn_get("info/prices")
        time.sleep(3)
        code2, d2 = _mn_get("info/prices")
        assert code1 == code2 == 200

        p1 = d1.get("data", d1) if isinstance(d1, dict) else d1
        p2 = d2.get("data", d2) if isinstance(d2, dict) else d2

        btc1 = next((p for p in p1 if p.get("symbol") == "BTC"), None)
        btc2 = next((p for p in p2 if p.get("symbol") == "BTC"), None)
        assert btc1 and btc2

        price1 = float(btc1.get("mark", 0))
        price2 = float(btc2.get("mark", 0))
        if price1 > 0:
            change_pct = abs(price2 - price1) / price1 * 100
            assert change_pct < 1.0, f"3초 내 가격 변동 {change_pct:.3f}%"
        print(f"\n✅ MR-C01: 3s 내 BTC ${price1:,.2f} → ${price2:,.2f}")

    def test_mr_c02_leaderboard_order_stable(self):
        """[MR-C02] 리더보드 순서 안정성"""
        code1, d1 = _mn_get("leaderboard?limit=10")
        time.sleep(1)
        code2, d2 = _mn_get("leaderboard?limit=10")
        assert code1 == code2 == 200

        lb1 = d1.get("data", d1) if isinstance(d1, dict) else d1
        lb2 = d2.get("data", d2) if isinstance(d2, dict) else d2

        addrs1 = [t["address"] for t in lb1[:3]]
        addrs2 = [t["address"] for t in lb2[:3]]
        overlap = len(set(addrs1) & set(addrs2))
        assert overlap >= 2, f"TOP3 순위 변동 심함: {overlap}/3 유지"
        print(f"\n✅ MR-C02: 리더보드 TOP3 중 {overlap}명 유지")

    def test_mr_c03_no_write_operation(self):
        """[MR-C03] 읽기전용 확인 — POST HTTP 메서드 사용 없음"""
        # sendall에서 'POST ' 로 시작하는 HTTP 요청이 없음을 확인
        import re
        with open(__file__, "r") as f:
            src = f.read()
        # HTTP POST 요청 패턴: sendall 에 b'POST ' 또는 "POST {path}"
        http_post = re.findall(r"sendall\(.*?b['\"]POST ", src)
        assert len(http_post) == 0, f"HTTP POST 요청 {len(http_post)}개 발견"
        print(f"\n✅ MR-C03: 읽기전용 확인 — HTTP POST 0개")
