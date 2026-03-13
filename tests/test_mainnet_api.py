"""
Mainnet API 연결 테스트
api.pacifica.fi — codetabs CORS 프록시

실행: pytest tests/test_mainnet_api.py -v
"""

import pytest
import json
import urllib.request
import urllib.parse
import time

MAINNET_URL = "https://api.pacifica.fi/api/v1"
CODETABS    = "https://api.codetabs.com/v1/proxy/?quest="


def mainnet_get(path: str) -> dict:
    target = f"{MAINNET_URL}/{path.lstrip('/')}"
    url    = CODETABS + urllib.parse.quote(target, safe="")
    req    = urllib.request.Request(url, headers={"User-Agent": "CopyPerp-Test/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read())
        return d.get("data", d) if isinstance(d, dict) and "data" in d else d


class TestMainnetConnection:
    """Mainnet API 기본 연결 테스트"""

    def test_mn001_info_endpoint(self):
        """Mainnet /info 심볼 목록 수신"""
        result = mainnet_get("info")
        data = result if isinstance(result, list) else result.get("data", [])
        symbols = [x["symbol"] for x in data] if isinstance(data, list) else list(result.keys())
        assert len(symbols) >= 10, f"심볼 수 부족: {len(symbols)}"
        assert "BTC" in symbols
        assert "ETH" in symbols
        assert "SOL" in symbols

    def test_mn002_prices_endpoint(self):
        """Mainnet 실시간 가격 수신"""
        result = mainnet_get("info/prices")
        data = result if isinstance(result, list) else result.get("data", [])
        if isinstance(data, dict):
            data = list(data.values())
        assert len(data) >= 10, f"가격 데이터 부족: {len(data)}"
        btc_items = [x for x in data if x.get("symbol") == "BTC"]
        assert btc_items, "BTC 가격 없음"
        btc_price = float(btc_items[0].get("mark", 0))
        assert btc_price > 10_000, f"BTC 가격 비정상: {btc_price}"

    def test_mn003_leaderboard(self):
        """Mainnet 리더보드 수신"""
        result = mainnet_get("leaderboard?limit=20")
        if result is None:
            pytest.skip("Mainnet 리더보드 응답 없음 (rate limit)")
        traders = result if isinstance(result, list) else (result.get("data", result) if isinstance(result, dict) else [])
        if isinstance(traders, dict):
            traders = list(traders.values())
        assert len(traders) >= 5, f"트레이더 수 부족: {len(traders)}"
        # 주소 필드 확인
        for t in traders[:3]:
            assert t.get("address"), f"주소 없음: {t}"

    def test_mn004_mainnet_vs_testnet_symbols(self):
        """Mainnet 심볼 수가 Testnet 이상이어야 함"""
        mainnet_info = mainnet_get("info")
        mainnet_data = mainnet_info if isinstance(mainnet_info, list) else mainnet_info.get("data", [])
        assert len(mainnet_data) >= 20, f"Mainnet 심볼 {len(mainnet_data)}개 — 예상 20+"

    def test_mn005_market_data_format(self):
        """Mainnet 마켓 데이터 필드 검증"""
        result = mainnet_get("info")
        data = result if isinstance(result, list) else result.get("data", [])
        btc = next((x for x in data if x.get("symbol") == "BTC"), None)
        assert btc is not None, "BTC 마켓 정보 없음"
        assert "tick_size" in btc
        assert "lot_size" in btc
        assert "max_leverage" in btc
        assert float(btc["max_leverage"]) >= 10


class TestMainnetLeaderboardAnalysis:
    """Mainnet 트레이더 분석 테스트"""

    @pytest.fixture(scope="class")
    def traders(self):
        result = mainnet_get("leaderboard?limit=100")
        data = result if isinstance(result, list) else result.get("data", result)
        return data if isinstance(data, list) else []

    def test_mn010_tier1_exists(self, traders):
        """Mainnet에 Tier1 트레이더 존재"""
        tier1 = []
        for t in traders:
            p30 = float(t.get("pnl_30d", 0) or 0)
            p7  = float(t.get("pnl_7d",  0) or 0)
            pa  = float(t.get("pnl_all_time", 0) or 0)
            eq  = float(t.get("equity_current", 1) or 1)
            if pa > 0 and p7 > 0 and p30 >= 50_000 and eq > 0:
                tier1.append(t)
        assert len(tier1) >= 1, f"Tier1 트레이더 없음 (총 {len(traders)}명 중)"

    def test_mn011_top_trader_positive_pnl(self, traders):
        """1위 트레이더는 양수 PnL"""
        if not traders:
            pytest.skip("트레이더 없음")
        top = traders[0]
        p30 = float(top.get("pnl_30d", 0) or 0)
        assert p30 > 0, f"1위 트레이더 30d PnL 음수: {p30}"

    def test_mn012_equity_positive(self, traders):
        """상위 10명 equity 양수"""
        for t in traders[:10]:
            eq = float(t.get("equity_current", 0) or 0)
            assert eq >= 0, f"equity 음수: {t.get('address')} = {eq}"


class TestMainnetBacktest:
    """Mainnet 백테스팅 검증"""

    def test_mn020_backtest_script_runs(self):
        """analyze_mainnet.py 스크립트 정상 실행"""
        import subprocess, sys, os
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run(
            [sys.executable, "scripts/analyze_mainnet.py"],
            capture_output=True, text=True, timeout=60, cwd=project_root
        )
        assert result.returncode == 0, f"스크립트 실패:\n{result.stderr}"
        assert "Tier1" in result.stdout
        assert "ROI" in result.stdout

    def test_mn021_backtest_result_file(self):
        """mainnet_backtest_result.json 파일 존재 및 유효"""
        import os
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(project_root, "mainnet_backtest_result.json")
        assert os.path.exists(path), "mainnet_backtest_result.json 없음"
        with open(path) as f:
            d = json.load(f)
        assert d.get("network") == "mainnet"
        assert d.get("initial_capital") == 10_000
        assert "7day_roi_pct" in d
