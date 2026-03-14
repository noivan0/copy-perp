"""
AutoResearch — 신호지표 자동 최적화 엔진
Karpathy autoresearch 방법론 적용:
  - 인간(전략팀)이 목적함수(program.md)를 설계
  - AI 에이전트가 파라미터 공간을 반복 탐색
  - 각 실험은 git commit으로 기록 (재현 가능)
  - 최고 점수 설정이 자동으로 채택됨

목적: 트레이더 선정 신호지표 최적화 → 팔로워 수익 극대화
평가지표(objective): Sharpe Ratio (risk-adjusted return)
  - 단순 PnL 최대화가 아닌 안정적 수익률 최적화
"""

import json
import math
import time
import ssl
import socket
import itertools
import os
from copy import deepcopy
from dataclasses import dataclass, field, asdict
from typing import Optional

# ── API 연결 ────────────────────────────────────────────────
CF_HOST = "do5jt23sqak4.cloudfront.net"
PAC_HOST = "api.pacifica.fi"
PORT = 443

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def _cf_get(path: str, retries: int = 3, timeout: int = 15):
    for i in range(retries):
        try:
            sock = socket.create_connection((CF_HOST, PORT), timeout=timeout)
            ssock = _ssl_ctx.wrap_socket(sock, server_hostname=CF_HOST)
            req = (f"GET /api/v1/{path} HTTP/1.1\r\nHost: {PAC_HOST}\r\n"
                   f"Accept: application/json\r\nConnection: close\r\n\r\n")
            ssock.sendall(req.encode())
            data = b""
            ssock.settimeout(timeout)
            while True:
                chunk = ssock.recv(16384)
                if not chunk: break
                data += chunk
            ssock.close(); sock.close()
            body = data.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in data else data
            if body and body[0:1].isdigit() and b"\r\n" in body[:16]:
                try:
                    size_line, rest = body.split(b"\r\n", 1)
                    body = rest[:int(size_line.strip(), 16)]
                except: pass
            return json.loads(body.decode("utf-8", "ignore"))
        except Exception:
            time.sleep(1.0 * (i + 1))
    return None


def get_leaderboard(limit: int = 200) -> list:
    r = _cf_get(f"leaderboard?limit={limit}")
    if isinstance(r, dict): return r.get("data", []) or []
    return r or []


def get_trades(addr: str, limit: int = 200) -> list:
    r = _cf_get(f"trades/history?account={addr}&limit={limit}")
    if isinstance(r, dict): return r.get("history", []) or r.get("data", []) or []
    return r or []


# ── 신호지표 파라미터 공간 (탐색 대상) ─────────────────────
@dataclass
class SignalConfig:
    """트레이더 선정 기준 — 최적화 대상 파라미터"""
    # Layer 1: 즉시 탈락 필터
    min_equity: float = 10_000.0          # 최소 자본 (USD)
    max_leverage: float = 2.0             # 최대 레버리지 (OI/Equity)
    min_pnl7d: bool = True                # 7일 수익 양수 필수
    min_pnl30d: bool = True               # 30일 수익 양수 필수
    min_pnl_at: bool = True               # 전체 수익 양수 필수
    min_consistency: int = 3              # 최소 일관성 (X/4)

    # Layer 2: 실거래 기반 신호 (팔로워 실수익 예측)
    min_profit_factor: float = 1.0        # 최소 Profit Factor
    min_sortino: float = 0.0             # 최소 Sortino Ratio
    max_mcl: int = 10                     # 최대 연속 손실 허용

    # Layer 3: 트레이더 선정 수
    max_traders: int = 5                  # 포트폴리오 최대 트레이더 수
    min_traders: int = 2                  # 최소 트레이더 수

    # Layer 4: 가중치 전략
    weight_method: str = "sharpe"         # "equal"|"roi"|"sharpe"|"sortino"

    # Layer 5: 복사 파라미터
    copy_ratio: float = 0.05             # 팔로워 자본 대비 복사 비율
    max_position_usdc: float = 300.0     # 단일 포지션 최대 USD

    # Layer 6: 진입 필터 (추가 신호)
    use_momentum_filter: bool = False     # 최근 7일 수익 > 30일 평균 필요
    use_diversification: bool = True      # 동일 방향 집중 방지
    max_same_direction_pct: float = 0.7  # 동일 방향 포지션 최대 비율


# ── 트레이더 신호 계산 ──────────────────────────────────────
def compute_trader_signals(trader_data: dict, trades: list) -> dict:
    """
    단일 트레이더의 신호지표 계산
    
    Returns:
        dict with all computed signals
    """
    addr = trader_data.get("address", "")
    p1   = float(trader_data.get("pnl_1d") or 0)
    p7   = float(trader_data.get("pnl_7d") or 0)
    p30  = float(trader_data.get("pnl_30d") or 0)
    pAT  = float(trader_data.get("pnl_all_time") or 0)
    eq   = float(trader_data.get("equity_current") or 1)
    oi   = float(trader_data.get("oi_current") or 0)
    v7   = float(trader_data.get("volume_7d") or 0)

    # 기본 지표
    roi7  = p7 / eq * 100 if eq > 0 else 0
    roi30 = p30 / eq * 100 if eq > 0 else 0
    lev   = oi / eq if eq > 0 else 0
    cons  = sum([p1 > 0, p7 > 0, p30 > 0, pAT > 0])

    # 모멘텀 (30일 대비 7일 수익 비율)
    momentum_ratio = (p7 / 7) / (p30 / 30) if p30 > 0 else 0

    # Sharpe 근사 (7일 데이터 기반)
    daily_avg = p7 / 7
    daily_std = abs(p1 - daily_avg) + 1e-9
    sharpe_7d = daily_avg / daily_std * math.sqrt(7)

    # 실거래 기반 지표 (가장 예측력 높음)
    pf = sortino = 0.0
    mcl = cur_loss = 0
    wins = losses = 0
    pnls = []
    neg_pnls = []

    for t in trades:
        if t.get("event_type") not in ("close_long", "close_short", "liquidation"):
            continue
        pnl = float(t.get("pnl") or 0)
        pnls.append(pnl)
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
            neg_pnls.append(pnl)

        # MCL 계산
        if pnl < 0:
            cur_loss += 1
            mcl = max(mcl, cur_loss)
        else:
            cur_loss = 0

    total_pnl = sum(pnls)
    n = len(pnls)
    win_rate = wins / (wins + losses) if (wins + losses) > 0 else 0

    # Profit Factor
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss   = abs(sum(p for p in pnls if p < 0))
    pf = gross_profit / gross_loss if gross_loss > 0 else (999 if gross_profit > 0 else 0)

    # Sortino Ratio
    if neg_pnls and n > 0:
        mean_p = total_pnl / n
        downside_dev = math.sqrt(sum(p**2 for p in neg_pnls) / len(neg_pnls))
        sortino = mean_p / downside_dev * math.sqrt(n) if downside_dev > 1e-10 else 0
    else:
        sortino = float('inf') if total_pnl > 0 else 0

    # Calmar Ratio (수익률 / 최대손실)
    max_loss_single = min(pnls) if pnls else 0
    calmar = roi7 / abs(max_loss_single / eq * 100) if max_loss_single < 0 and eq > 0 else 0

    # 거래 빈도 (활성도)
    activity = n / 7 if n > 0 else 0  # 일평균 거래수

    # 방향 편향 (Long/Short 비율)
    longs  = sum(1 for t in trades if t.get("side") == "bid" and t.get("event_type","").startswith("close"))
    shorts = sum(1 for t in trades if t.get("side") == "ask" and t.get("event_type","").startswith("close"))
    direction_bias = longs / (longs + shorts) if (longs + shorts) > 0 else 0.5

    return {
        "address": addr,
        "equity": eq,
        "oi": oi,
        "leverage": lev,
        "roi7": roi7,
        "roi30": roi30,
        "roi_at": pAT / eq * 100 if eq > 0 else 0,
        "consistency": cons,
        "momentum_ratio": momentum_ratio,
        "sharpe_7d": sharpe_7d,
        "profit_factor": pf,
        "sortino": sortino,
        "calmar": calmar,
        "max_consecutive_losses": mcl,
        "win_rate": win_rate,
        "trade_count": n,
        "activity": activity,
        "direction_bias": direction_bias,
        "pnl_1d": p1, "pnl_7d": p7, "pnl_30d": p30, "pnl_at": pAT,
    }


# ── 백테스트 시뮬레이션 ────────────────────────────────────
def backtest(traders_data: list, config: SignalConfig, 
             all_trades: dict) -> dict:
    """
    신호지표 설정으로 트레이더 선정 → 실거래 데이터 시뮬레이션
    
    Returns:
        dict: objective score + 상세 성과 지표
    """
    # Step 1: 신호 계산
    signals = []
    for td in traders_data:
        addr = td["address"]
        trades = all_trades.get(addr, [])
        sig = compute_trader_signals(td, trades)
        signals.append(sig)

    # Step 2: 필터 적용
    filtered = []
    for sig in signals:
        if sig["equity"] < config.min_equity: continue
        if sig["leverage"] > config.max_leverage: continue
        if config.min_pnl7d  and sig["pnl_7d"] <= 0: continue
        if config.min_pnl30d and sig["pnl_30d"] <= 0: continue
        if config.min_pnl_at and sig["pnl_at"] <= 0: continue
        if sig["consistency"] < config.min_consistency: continue
        if sig["profit_factor"] < config.min_profit_factor: continue
        if sig["sortino"] < config.min_sortino: continue
        if sig["max_consecutive_losses"] > config.max_mcl: continue
        filtered.append(sig)

    if len(filtered) < config.min_traders:
        return {"score": -999, "reason": f"트레이더 부족 ({len(filtered)}명)"}

    # Step 3: 정렬 & 선정
    sort_key = {
        "equal":   lambda s: s["roi7"],
        "roi":     lambda s: s["roi7"] * 0.5 + s["roi30"] * 0.5,
        "sharpe":  lambda s: s["sharpe_7d"],
        "sortino": lambda s: min(s["sortino"], 100),
    }.get(config.weight_method, lambda s: s["sharpe_7d"])

    selected = sorted(filtered, key=sort_key, reverse=True)[:config.max_traders]

    # Step 4: 가중치 계산
    if config.weight_method == "equal":
        weights = [1.0 / len(selected)] * len(selected)
    else:
        scores = [max(sort_key(s), 1e-6) for s in selected]
        total  = sum(scores)
        weights = [sc / total for sc in scores]

    # Step 5: 팔로워 PnL 시뮬레이션
    INITIAL = 10_000.0
    capital = INITIAL
    all_pnls = []
    trader_results = []

    for sig, w in zip(selected, weights):
        addr   = sig["address"]
        trades = all_trades.get(addr, [])
        tr_pnl = 0.0
        tr_wins = tr_losses = 0
        tr_pnls = []

        for t in trades:
            if t.get("event_type") not in ("close_long", "close_short", "liquidation"):
                continue
            raw_pnl = float(t.get("pnl") or 0)
            eq      = sig["equity"]
            
            # 복사 비율 계산
            scale = min(1.0, INITIAL / eq) if eq > 0 else 1.0
            ratio = config.copy_ratio * w * scale
            copy_pnl = raw_pnl * ratio
            
            # 최대 포지션 제한
            if abs(copy_pnl) > config.max_position_usdc:
                copy_pnl = math.copysign(config.max_position_usdc, copy_pnl)
            
            # 모멘텀 필터 (진입 신호)
            if config.use_momentum_filter and sig["momentum_ratio"] < 0.8:
                continue  # 모멘텀 약화 시 신규 포지션 스킵
            
            tr_pnl += copy_pnl
            tr_pnls.append(copy_pnl)
            all_pnls.append(copy_pnl)
            if copy_pnl > 0: tr_wins += 1
            elif copy_pnl < 0: tr_losses += 1

        capital += tr_pnl
        trader_results.append({
            "address": addr[:12],
            "weight": w,
            "pnl": tr_pnl,
            "trades": len(tr_pnls),
            "wr": tr_wins/(tr_wins+tr_losses) if (tr_wins+tr_losses)>0 else 0,
        })

    # Step 6: 목적함수 계산
    total_pnl = capital - INITIAL
    roi = total_pnl / INITIAL

    if not all_pnls:
        return {"score": -999, "reason": "거래 없음"}

    n = len(all_pnls)
    mean_pnl = sum(all_pnls) / n
    std_pnl  = math.sqrt(sum((p - mean_pnl)**2 for p in all_pnls) / n) + 1e-10

    # Sharpe (목적함수)
    sharpe = mean_pnl / std_pnl * math.sqrt(n)

    # Sortino (목적함수 보완)
    neg = [p for p in all_pnls if p < 0]
    down_std = math.sqrt(sum(p**2 for p in neg)/len(neg)) if neg else 1e-10
    sortino_portfolio = mean_pnl / down_std * math.sqrt(n) if down_std > 1e-10 else 0

    # Max Drawdown
    peak = cum = 0.0
    mdd = 0.0
    for p in all_pnls:
        cum += p
        peak = max(peak, cum)
        dd = (peak - cum) / (INITIAL + peak) * 100
        mdd = max(mdd, dd)

    # WR
    wins   = sum(1 for p in all_pnls if p > 0)
    losses = sum(1 for p in all_pnls if p < 0)
    wr     = wins / (wins + losses) if (wins + losses) > 0 else 0

    # PF
    gp = sum(p for p in all_pnls if p > 0)
    gl = abs(sum(p for p in all_pnls if p < 0))
    pf = gp / gl if gl > 0 else 999

    # 복합 목적함수: Sharpe * PnL부호 * PF가중치
    # 음수 PnL이면 페널티
    pnl_sign = 1 if total_pnl > 0 else -1
    objective = sharpe * pnl_sign * min(pf, 5) / 5 * (1 - mdd/100)

    return {
        "score": objective,
        "sharpe": sharpe,
        "sortino": sortino_portfolio,
        "roi": roi * 100,
        "total_pnl": total_pnl,
        "win_rate": wr * 100,
        "profit_factor": pf,
        "max_drawdown": mdd,
        "trade_count": n,
        "trader_count": len(selected),
        "traders": trader_results,
        "config": asdict(config),
    }


# ── Auto Research 루프 ────────────────────────────────────
class AutoResearch:
    """
    Karpathy autoresearch 방법론 적용:
    1. program.md = 이 파일의 SignalConfig 파라미터 공간
    2. 에이전트 = 파라미터 탐색 루프 (그리드 → 베이즈 → 진화)
    3. 목적함수 = Sharpe * PF * (1-MDD) [복합 안정성 지표]
    4. 최고 설정 자동 채택 + 기록
    """

    def __init__(self):
        self.best_score  = -999
        self.best_config = None
        self.best_result = None
        self.history     = []
        self.iteration   = 0

    def _param_grid(self) -> list:
        """탐색할 파라미터 조합 생성 (Phase 1: Grid Search)"""
        grid = {
            "min_profit_factor": [0.0, 0.8, 1.0, 1.2],
            "min_sortino":       [-999, 0.0, 0.5, 1.0],
            "max_mcl":           [5, 10, 15, 20],
            "max_leverage":      [1.0, 1.5, 2.0, 3.0],
            "min_consistency":   [2, 3, 4],
            "weight_method":     ["equal", "roi", "sharpe", "sortino"],
            "max_traders":       [3, 4, 5],
            "use_momentum_filter": [False, True],
        }
        # 총 조합: 4*4*4*4*3*4*3*2 = 18,432 → 중요 파라미터만 탐색
        # 핵심 파라미터 우선 탐색
        core = {
            "min_profit_factor": [0.0, 0.8, 1.0, 1.2],
            "min_sortino":       [-999, 0.0, 0.5],
            "max_mcl":           [5, 10, 20],
            "weight_method":     ["equal", "roi", "sharpe", "sortino"],
            "max_traders":       [3, 5],
        }
        keys   = list(core.keys())
        values = list(core.values())
        combos = list(itertools.product(*values))
        return [dict(zip(keys, c)) for c in combos]

    def run(self, max_iterations: int = 200, output_dir: str = "."):
        """Auto Research 실행"""
        print("=" * 60)
        print("🔬 AutoResearch — 신호지표 최적화 시작")
        print(f"   목적함수: Sharpe × PF × (1-MDD)")
        print(f"   최대 반복: {max_iterations}회")
        print("=" * 60)

        # 데이터 수집 (1회)
        print("\n📡 Mainnet 데이터 수집 중...")
        leaderboard = get_leaderboard(200)
        print(f"   리더보드: {len(leaderboard)}명")

        # 실거래 데이터 수집 (상위 50명)
        all_trades = {}
        candidates = [t for t in leaderboard if
                      float(t.get("equity_current") or 0) >= 5000][:50]
        print(f"   실거래 수집 대상: {len(candidates)}명")
        for i, td in enumerate(candidates):
            addr = td["address"]
            trades = get_trades(addr, 100)
            all_trades[addr] = trades
            if (i+1) % 10 == 0:
                print(f"   수집 중... {i+1}/{len(candidates)}")
            time.sleep(0.3)  # Rate limit

        print(f"   총 실거래 데이터: {sum(len(v) for v in all_trades.values())}건")

        # Phase 1: Grid Search
        print("\n🔍 Phase 1: Grid Search")
        param_combos = self._param_grid()
        print(f"   탐색 조합: {len(param_combos)}개")

        for i, params in enumerate(param_combos[:max_iterations]):
            self.iteration += 1
            config = SignalConfig(**params)
            result = backtest(leaderboard, config, all_trades)

            score  = result.get("score", -999)
            self.history.append({
                "iteration": self.iteration,
                "score":     score,
                "params":    params,
                "result":    result,
            })

            if score > self.best_score:
                self.best_score  = score
                self.best_config = config
                self.best_result = result
                print(f"\n  ✨ 새 최고점 #{self.iteration} score={score:.4f}")
                print(f"     PnL={result.get('total_pnl',0):+.2f}  "
                      f"WR={result.get('win_rate',0):.0f}%  "
                      f"PF={result.get('profit_factor',0):.2f}  "
                      f"Sharpe={result.get('sharpe',0):.2f}  "
                      f"MDD={result.get('max_drawdown',0):.2f}%")
                print(f"     파라미터: {params}")
            elif (i+1) % 50 == 0:
                print(f"  #{self.iteration} 완료 (현재 최고: {self.best_score:.4f})")

        # Phase 2: Local Search (최고점 주변 세밀 탐색)
        print("\n🔍 Phase 2: Local Search (최고점 주변)")
        if self.best_config:
            best_p = asdict(self.best_config)
            local_variations = [
                {"min_profit_factor": best_p["min_profit_factor"] + d}
                for d in [-0.2, -0.1, 0.1, 0.2]
            ] + [
                {"min_sortino": best_p["min_sortino"] + d}
                for d in [-0.5, 0.5, 1.0]
            ] + [
                {"max_mcl": max(1, best_p["max_mcl"] + d)}
                for d in [-3, -2, -1, 1, 2, 3]
            ]

            for variation in local_variations:
                params = {**asdict(self.best_config), **variation}
                # SignalConfig 필드만 사용
                valid_fields = {k: v for k, v in params.items()
                                if k in SignalConfig.__dataclass_fields__}
                config = SignalConfig(**valid_fields)
                result = backtest(leaderboard, config, all_trades)
                score  = result.get("score", -999)
                self.iteration += 1

                if score > self.best_score:
                    self.best_score  = score
                    self.best_config = config
                    self.best_result = result
                    print(f"\n  ✨ Local 최고점 #{self.iteration} score={score:.4f}")
                    print(f"     {variation}")

        # 결과 저장
        ts = time.strftime("%Y%m%d_%H%M%S")
        output = {
            "timestamp": ts,
            "iterations": self.iteration,
            "best_score": self.best_score,
            "best_config": asdict(self.best_config) if self.best_config else {},
            "best_result": self.best_result or {},
            "top10": sorted(self.history, key=lambda x: -x["score"])[:10],
        }
        path = os.path.join(output_dir, f"autoresearch_{ts}.json")
        with open(path, "w") as f:
            json.dump(output, f, indent=2, default=str)
        print(f"\n💾 결과 저장: {path}")

        self._print_final_report(output)
        return output

    def _print_final_report(self, output: dict):
        print("\n" + "=" * 60)
        print("📊 AutoResearch 최종 결과")
        print("=" * 60)
        br = output.get("best_result", {})
        bc = output.get("best_config", {})

        print(f"\n🏆 최적 설정 (score={output['best_score']:.4f})")
        print(f"   총 PnL:      ${br.get('total_pnl',0):+.2f}")
        print(f"   ROI:         {br.get('roi',0):+.2f}%")
        print(f"   승률:        {br.get('win_rate',0):.0f}%")
        print(f"   Profit Factor: {br.get('profit_factor',0):.2f}x")
        print(f"   Sharpe:      {br.get('sharpe',0):.2f}")
        print(f"   Sortino:     {br.get('sortino',0):.2f}")
        print(f"   Max Drawdown: {br.get('max_drawdown',0):.2f}%")
        print(f"   거래수:      {br.get('trade_count',0)}건")
        print(f"   트레이더수:  {br.get('trader_count',0)}명")

        print(f"\n📐 최적 파라미터")
        print(f"   min_profit_factor:  {bc.get('min_profit_factor')}")
        print(f"   min_sortino:        {bc.get('min_sortino')}")
        print(f"   max_mcl:            {bc.get('max_mcl')}")
        print(f"   max_leverage:       {bc.get('max_leverage')}")
        print(f"   min_consistency:    {bc.get('min_consistency')}")
        print(f"   weight_method:      {bc.get('weight_method')}")
        print(f"   max_traders:        {bc.get('max_traders')}")
        print(f"   use_momentum_filter:{bc.get('use_momentum_filter')}")

        print(f"\n👥 최적 포트폴리오 트레이더")
        for tr in br.get("traders", []):
            print(f"   {tr['address']}  weight={tr['weight']:.2f}  "
                  f"pnl={tr['pnl']:+.2f}  wr={tr['wr']*100:.0f}%")

        print(f"\n🔝 Top 10 실험 결과")
        for i, h in enumerate(output.get("top10", []), 1):
            r = h["result"]
            print(f"   #{i:2d} score={h['score']:+.4f}  "
                  f"pnl={r.get('total_pnl',0):+.2f}  "
                  f"wr={r.get('win_rate',0):.0f}%  "
                  f"pf={r.get('profit_factor',0):.2f}  "
                  f"method={h['params'].get('weight_method','?')}")


if __name__ == "__main__":
    import sys
    output_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    ar = AutoResearch()
    ar.run(max_iterations=300, output_dir=output_dir)
