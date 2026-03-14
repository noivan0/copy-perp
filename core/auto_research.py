"""
Auto-Research: TraderScore 신호지표 자동 최적화

Karpathy autoresearch 방법론 적용:
- 파라미터 공간을 자율 탐색 (~수백 실험)
- 각 실험 결과(페이퍼트레이딩 PnL)로 다음 실험 계획
- 개선사항이 additive하게 쌓임
- 논문 기반 이론 + 실증으로 검증

연구 기반:
- Apesteguia et al. 2020 (Mgmt Sci): copy trading에서 risk-taking 편향
- Oehler & Schneider 2022: signal provider gambling 성향
- Perpetual futures funding rate 영향 (arXiv:2212.06888)
- Social signals in Bitcoin trading (arXiv:1506.01513)
- Pelster & Hofmann 2018: eToro 플랫폼 복사 vs 포트폴리오 분산

핵심 발견 (논문 종합):
1. 일관성 > 고수익: 변동성 높은 트레이더를 복사한 팔로워는 손실 경향
2. 레버리지 < 5x: 고레버리지 signal provider는 팔로워에게 불리
3. 단기 성과 과대평가: 1일 수익은 노이즈, 30일 일관성이 예측력 있음
4. 거래량 회전율: HFT 성향 트레이더는 복사 불가 (실행 지연)
5. Funding rate 방향: 포지션이 funding에 유리한 방향인지 (추가 수익)
"""

import json
import os
import time
import random
import ssl
import socket
import math
from dataclasses import dataclass, field, asdict
from typing import Optional
from collections import defaultdict

# ── 설정 ──────────────────────────────────────────────

CF_HOST = 'do5jt23sqak4.cloudfront.net'
PAC_HOST = 'api.pacifica.fi'

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

INITIAL_CAPITAL = 10_000.0
COPY_RATIO = 0.05        # 5% per position
MAX_POS_USDC = 300.0
FEE_RATE = 0.0006        # 0.06% taker fee


# ── 데이터 수집 ────────────────────────────────────────

def _raw_get(path: str) -> dict:
    sock = socket.create_connection((CF_HOST, 443), timeout=15)
    ssock = _ssl_ctx.wrap_socket(sock, server_hostname=CF_HOST)
    req = f'GET /api/v1/{path} HTTP/1.1\r\nHost: {PAC_HOST}\r\nAccept: application/json\r\nConnection: close\r\n\r\n'
    ssock.sendall(req.encode())
    data = b''
    ssock.settimeout(15)
    while True:
        chunk = ssock.recv(32768)
        if not chunk:
            break
        data += chunk
    ssock.close()
    _, body = data.split(b'\r\n\r\n', 1)
    return json.loads(body.decode('utf-8', 'ignore'))


def fetch_leaderboard(limit: int = 100) -> list[dict]:
    r = _raw_get(f"leaderboard?limit={limit}")
    return r.get("data", []) if r.get("success") else []


def fetch_positions(account: str) -> list[dict]:
    r = _raw_get(f"positions?account={account}")
    return r.get("data", []) if r.get("success") else []


def fetch_trades(account: str, limit: int = 40) -> list[dict]:
    r = _raw_get(f"trades?account={account}&limit={limit}")
    return r.get("data", []) if r.get("success") else []


# ── 논문 기반 지표 계산 ────────────────────────────────

@dataclass
class TraderMetrics:
    address: str
    alias: str = ""

    # 기본 leaderboard 필드
    pnl_1d: float = 0.0
    pnl_7d: float = 0.0
    pnl_30d: float = 0.0
    pnl_all: float = 0.0
    equity: float = 0.0
    oi: float = 0.0
    vol_1d: float = 0.0
    vol_7d: float = 0.0
    vol_30d: float = 0.0

    # 계산 지표
    roi_30d: float = 0.0
    roi_7d: float = 0.0
    roi_1d: float = 0.0
    leverage: float = 0.0
    turnover_30d: float = 0.0      # vol_30d / equity (회전율)
    consistency: float = 0.0       # 3기간 모두 양수인지
    momentum: float = 0.0          # 7d roi / 30d roi (최근 가속)
    avg_hold_hours: float = 0.0    # 평균 보유시간 (positions)
    funding_exposure: float = 0.0  # 현재 펀딩 노출 ($)
    liq_distance_pct: float = 0.0  # 청산가까지 거리 %
    position_count: int = 0
    unique_symbols: int = 0

    # 논문 기반 추가 지표
    pnl_acceleration: float = 0.0  # (7d - 30d*7/30) — 최근 초과 성과 (Momentum factor)
    risk_adjusted_roi: float = 0.0 # roi / (leverage+1) — 레버리지 보정 수익
    copyability: float = 0.0       # 1 - min(turnover/100, 1) — 복사 가능성
    pos_bias: float = 0.0          # long/(long+short) — 방향 편향도
    avg_position_size: float = 0.0 # equity 대비 평균 포지션 크기


def compute_metrics(row: dict, positions: list[dict] = None) -> TraderMetrics:
    """leaderboard row + positions → TraderMetrics"""
    addr = row.get("address", "")
    equity = float(row.get("equity_current", 0) or 0)
    oi = float(row.get("oi_current", 0) or 0)

    m = TraderMetrics(
        address=addr,
        alias=row.get("username", "") or addr[:12],
        pnl_1d=float(row.get("pnl_1d", 0) or 0),
        pnl_7d=float(row.get("pnl_7d", 0) or 0),
        pnl_30d=float(row.get("pnl_30d", 0) or 0),
        pnl_all=float(row.get("pnl_all_time", 0) or 0),
        equity=equity,
        oi=oi,
        vol_1d=float(row.get("volume_1d", 0) or 0),
        vol_7d=float(row.get("volume_7d", 0) or 0),
        vol_30d=float(row.get("volume_30d", 0) or 0),
    )

    # ROI 계산
    if equity > 0:
        cost_basis = equity - m.pnl_30d
        if cost_basis > 0:
            m.roi_30d = m.pnl_30d / cost_basis * 100
        cost_7 = equity - m.pnl_7d
        if cost_7 > 0:
            m.roi_7d = m.pnl_7d / cost_7 * 100
        cost_1 = equity - m.pnl_1d
        if cost_1 > 0:
            m.roi_1d = m.pnl_1d / cost_1 * 100

    # 레버리지
    if equity > 0:
        m.leverage = oi / equity

    # 회전율
    if equity > 0:
        m.turnover_30d = m.vol_30d / equity

    # 일관성 (3기간 모두 양수)
    pos_count = sum(1 for v in [m.pnl_1d, m.pnl_7d, m.pnl_30d] if v > 0)
    m.consistency = pos_count / 3.0

    # 모멘텀: 7d 수익이 30d 평균 대비 가속인지
    daily_30 = m.pnl_30d / 30 if m.pnl_30d != 0 else 0
    daily_7 = m.pnl_7d / 7 if m.pnl_7d != 0 else 0
    if daily_30 != 0:
        m.momentum = daily_7 / abs(daily_30)
    
    # 논문 기반 지표
    # PnL 가속도 (최근 7일이 30일 평균보다 얼마나 좋은지)
    m.pnl_acceleration = daily_7 - daily_30

    # 레버리지 보정 ROI (위험 조정 수익)
    m.risk_adjusted_roi = m.roi_30d / (m.leverage + 1) if m.leverage >= 0 else 0

    # 복사 가능성 (회전율이 낮을수록 높음)
    m.copyability = max(0.0, 1.0 - min(m.turnover_30d / 100.0, 1.0))

    # Positions 기반 지표
    if positions:
        m.position_count = len(positions)
        symbols = set()
        longs = 0
        total_margin = 0.0
        total_dist = 0.0
        now_ms = time.time() * 1000

        for pos in positions:
            sym = pos.get("symbol", "?")
            symbols.add(sym)
            side = pos.get("side", "")
            if side == "bid":
                longs += 1

            margin = float(pos.get("margin", 0) or 0)
            total_margin += margin

            # 청산가 거리
            entry = float(pos.get("entry_price", 0) or 0)
            liq = float(pos.get("liquidation_price", 0) or 0)
            if entry > 0 and liq > 0:
                dist = abs(entry - liq) / entry * 100
                total_dist += dist

            # 펀딩
            funding = float(pos.get("funding", 0) or 0)
            m.funding_exposure += funding

            # 보유시간
            created = pos.get("created_at", 0) or 0
            hold_ms = now_ms - float(created)
            m.avg_hold_hours += hold_ms / 3_600_000

        m.unique_symbols = len(symbols)
        if positions:
            m.avg_hold_hours /= len(positions)
            if total_dist > 0:
                m.liq_distance_pct = total_dist / len(positions)

        total_dirs = len(positions)
        m.pos_bias = longs / total_dirs if total_dirs > 0 else 0.5

        if equity > 0 and positions:
            m.avg_position_size = total_margin / len(positions) / equity * 100

    return m


# ── TraderScore 파라미터 공간 ─────────────────────────

@dataclass
class ScoreParams:
    """최적화할 파라미터 공간 — 논문 기반 초기값"""
    # 가중치 (합계가 1이 되도록 normalize)
    w_consistency: float = 0.35    # Oehler 2022: 일관성이 가장 중요
    w_momentum: float = 0.20       # 최근 추세 (가속도)
    w_risk_adj_roi: float = 0.20   # 레버리지 보정 수익
    w_copyability: float = 0.15    # 복사 가능성 (회전율 역수)
    w_liq_safety: float = 0.10     # 청산가 안전거리

    # 필터 임계값
    min_equity: float = 50_000.0          # 최소 자본 ($50k)
    max_leverage: float = 10.0            # 최대 레버리지
    max_turnover: float = 100.0           # 최대 회전율 (x)
    min_pnl_30d: float = 3_000.0          # 최소 30일 PnL
    min_consistency: float = 0.33         # 최소 1/3 기간 양수
    min_copyability: float = 0.30         # 최소 복사 가능성

    # 모멘텀 파라미터
    momentum_cap: float = 3.0             # 모멘텀 상한 (과도한 상승 제한)
    momentum_floor: float = -1.0          # 모멘텀 하한 (너무 빠른 하락 제거)

    # 복사 설정
    n_traders: int = 5                    # 복사할 트레이더 수
    max_pos_usdc: float = 300.0           # 최대 포지션 크기


def score_trader(m: TraderMetrics, p: ScoreParams) -> float:
    """논문 기반 TraderScore 계산 (0~1)"""

    # ── 하드 필터 ──────────────────────────
    if m.equity < p.min_equity:
        return 0.0
    if m.pnl_30d < p.min_pnl_30d:
        return 0.0
    if m.pnl_all <= 0:
        return 0.0
    if m.leverage > p.max_leverage:
        return 0.0
    if m.turnover_30d > p.max_turnover:
        return 0.0
    if m.consistency < p.min_consistency:
        return 0.0
    if m.copyability < p.min_copyability:
        return 0.0

    # ── 연속 지표 계산 (0~1) ──────────────
    # 1. 일관성 (0~1, 이미 0-1 범위)
    s_consistency = m.consistency

    # 2. 모멘텀 (클램핑 + 정규화)
    mom = max(p.momentum_floor, min(p.momentum_cap, m.momentum))
    s_momentum = (mom - p.momentum_floor) / (p.momentum_cap - p.momentum_floor)

    # 3. 레버리지 보정 ROI (log scale, 상한 100%)
    rar = max(0.0, min(m.risk_adjusted_roi, 100.0))
    s_risk_adj_roi = math.log1p(rar) / math.log1p(100.0)

    # 4. 복사 가능성 (이미 0~1)
    s_copyability = m.copyability

    # 5. 청산가 안전거리 (멀수록 좋음, 상한 50%)
    liq_dist = min(m.liq_distance_pct, 50.0)
    s_liq_safety = liq_dist / 50.0

    # ── 가중 합산 ───────────────────────────
    w_total = (p.w_consistency + p.w_momentum + p.w_risk_adj_roi
               + p.w_copyability + p.w_liq_safety)
    if w_total <= 0:
        return 0.0

    score = (
        p.w_consistency  * s_consistency +
        p.w_momentum     * s_momentum +
        p.w_risk_adj_roi * s_risk_adj_roi +
        p.w_copyability  * s_copyability +
        p.w_liq_safety   * s_liq_safety
    ) / w_total

    return round(min(1.0, max(0.0, score)), 4)


def select_traders(metrics: list[TraderMetrics], p: ScoreParams) -> list[tuple[TraderMetrics, float]]:
    """파라미터 기반 트레이더 선별"""
    scored = []
    for m in metrics:
        s = score_trader(m, p)
        if s > 0:
            scored.append((m, s))
    scored.sort(key=lambda x: -x[1])
    return scored[:p.n_traders]


# ── 페이퍼트레이딩 시뮬레이터 (빠른 버전) ─────────────

class FastPaperSim:
    """
    과거 closed_trades 데이터로 빠른 시뮬레이션
    실제 API 호출 없이 기존 데이터로 백테스트

    주의: closed_trades의 trader 필드는 address 앞 8자 (short form)
    """
    def __init__(self, closed_trades: list[dict], capital: float = INITIAL_CAPITAL):
        self.trades = closed_trades
        self.capital = capital
        # short → full address 역매핑 (trades 내에서 자동 구성)
        self._short_set: set[str] = set(t.get("trader","") for t in closed_trades)

    def _to_short(self, full_addr: str) -> str:
        return full_addr[:8]

    def run(self, selected_traders: list[str], params: ScoreParams) -> dict:
        equity = self.capital
        wins = 0
        losses = 0
        total_pnl = 0.0
        peak = equity

        # full address → short (8자) 변환
        trader_set = set(self._to_short(addr) for addr in selected_traders)

        for t in self.trades:
            trader = t.get("trader", "")
            if trader not in trader_set:
                continue

            raw_pnl = float(t.get("raw_pnl", 0) or 0)
            amount = float(t.get("amount", 0) or 0)
            price = float(t.get("price", 0) or 0)
            pos_usdc = min(amount * price * COPY_RATIO, params.max_pos_usdc)
            if pos_usdc < 1.0:
                continue

            # copy_pnl 비례 계산
            copy_ratio_applied = pos_usdc / (amount * price) if (amount * price) > 0 else 0
            copy_pnl = raw_pnl * copy_ratio_applied
            fee = pos_usdc * FEE_RATE

            net_pnl = copy_pnl - fee
            equity += net_pnl
            total_pnl += net_pnl
            peak = max(peak, equity)

            if net_pnl > 0:
                wins += 1
            else:
                losses += 1

        total = wins + losses
        mdd = (peak - equity) / peak * 100 if peak > 0 else 0

        return {
            "roi_pct": total_pnl / self.capital * 100,
            "win_rate": wins / total * 100 if total > 0 else 0,
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "mdd_pct": mdd,
            "sharpe_proxy": total_pnl / (abs(total_pnl) + 1),  # 단순 방향성 지표
        }


# ── Auto-Research 엔진 ────────────────────────────────

class AutoResearch:
    """
    Karpathy autoresearch 방법론:
    1. 기준 파라미터로 시작
    2. 한 번에 하나씩 파라미터 변경
    3. 개선되면 채택, 아니면 폐기
    4. 개선 이력으로 다음 실험 계획
    5. 수렴할 때까지 반복
    """
    def __init__(self, metrics: list[TraderMetrics], sim: FastPaperSim):
        self.metrics = metrics
        self.sim = sim
        self.history: list[dict] = []
        self.best_params = ScoreParams()
        self.best_score = -999.0
        self.experiment_count = 0

    def _evaluate(self, p: ScoreParams) -> float:
        """파라미터 → 페이퍼트레이딩 점수 (최대화 목표)"""
        selected = select_traders(self.metrics, p)
        if not selected:
            return -100.0

        trader_addrs = [m.address for m, _ in selected]
        result = self.sim.run(trader_addrs, p)

        # 복합 목표: ROI × WinRate/50 - MDD/5
        # 논문 기반: 팔로워 경험에서 WR이 ROI보다 심리적으로 중요
        roi = result["roi_pct"]
        wr = result["win_rate"]
        mdd = result["mdd_pct"]
        n_trades = result["total_trades"]

        if n_trades < 5:
            return -50.0  # 거래 너무 적으면 패널티

        # 목적함수: Sharpe-like (수익성 + 안정성 + WR)
        objective = roi * (wr / 50.0) - mdd * 0.5
        return objective

    def _mutate(self, p: ScoreParams, field: str, delta: float) -> ScoreParams:
        """파라미터 하나 변경"""
        import copy
        new_p = copy.deepcopy(p)
        current = getattr(new_p, field)
        setattr(new_p, field, current + delta)
        return new_p

    def _clamp_params(self, p: ScoreParams) -> ScoreParams:
        """파라미터 유효 범위 보장"""
        p.w_consistency = max(0.05, min(0.7, p.w_consistency))
        p.w_momentum = max(0.05, min(0.5, p.w_momentum))
        p.w_risk_adj_roi = max(0.05, min(0.5, p.w_risk_adj_roi))
        p.w_copyability = max(0.05, min(0.5, p.w_copyability))
        p.w_liq_safety = max(0.0, min(0.3, p.w_liq_safety))
        p.min_equity = max(10_000, min(200_000, p.min_equity))
        p.max_leverage = max(1.0, min(20.0, p.max_leverage))
        p.max_turnover = max(10.0, min(300.0, p.max_turnover))
        p.min_pnl_30d = max(0, min(50_000, p.min_pnl_30d))
        p.momentum_cap = max(1.0, min(10.0, p.momentum_cap))
        p.n_traders = max(1, min(10, int(p.n_traders)))
        p.max_pos_usdc = max(50.0, min(1000.0, p.max_pos_usdc))
        return p

    def run(self, n_experiments: int = 200, verbose: bool = True) -> ScoreParams:
        """Auto-research 실행"""
        # 초기 평가
        self.best_score = self._evaluate(self.best_params)
        if verbose:
            print(f"초기 점수: {self.best_score:.4f}")
            print(f"초기 파라미터: {asdict(self.best_params)}")
            print()

        # 탐색할 파라미터와 변화량 정의
        search_space = [
            # (field, [delta 목록])
            ("w_consistency",   [-0.05, +0.05, -0.10, +0.10]),
            ("w_momentum",      [-0.05, +0.05, -0.10, +0.10]),
            ("w_risk_adj_roi",  [-0.05, +0.05, -0.10, +0.10]),
            ("w_copyability",   [-0.05, +0.05, -0.10, +0.10]),
            ("w_liq_safety",    [-0.02, +0.02, -0.05, +0.05]),
            ("min_equity",      [-10000, +10000, -25000, +25000]),
            ("max_leverage",    [-1.0, +1.0, -2.0, +2.0, -3.0]),
            ("max_turnover",    [-10, +10, -20, +20, -30]),
            ("min_pnl_30d",     [-1000, +1000, -2000, +2000]),
            ("momentum_cap",    [-0.5, +0.5, -1.0, +1.0]),
            ("momentum_floor",  [-0.25, +0.25]),
            ("n_traders",       [-1, +1, -2, +2]),
            ("max_pos_usdc",    [-50, +50, -100, +100]),
        ]

        improvements = []
        no_improve_streak = 0

        for exp_i in range(n_experiments):
            self.experiment_count += 1

            # 탐색 전략: 초반은 넓게, 후반은 최근 개선된 파라미터 집중
            if exp_i < 50 or no_improve_streak > 20:
                # 랜덤 파라미터 선택 (exploration)
                field, deltas = random.choice(search_space)
                delta = random.choice(deltas)
            else:
                # 최근 개선된 파라미터 근처 탐색 (exploitation)
                if improvements:
                    recent = improvements[-min(5, len(improvements)):]
                    field = random.choice(recent)["field"]
                    _, deltas = next(s for s in search_space if s[0] == field)
                    delta = random.choice(deltas) * 0.5  # 더 작은 보폭
                else:
                    field, deltas = random.choice(search_space)
                    delta = random.choice(deltas)

            candidate = self._mutate(self.best_params, field, delta)
            candidate = self._clamp_params(candidate)
            score = self._evaluate(candidate)

            record = {
                "exp": exp_i + 1,
                "field": field,
                "delta": delta,
                "score": score,
                "improved": score > self.best_score,
            }
            self.history.append(record)

            if score > self.best_score:
                improvement = score - self.best_score
                self.best_score = score
                self.best_params = candidate
                improvements.append({"field": field, "delta": delta, "gain": improvement})
                no_improve_streak = 0
                if verbose:
                    print(f"  ✅ Exp{exp_i+1:3d}: {field:20s} {delta:+.4f} → score={score:.4f} (+{improvement:.4f})")
            else:
                no_improve_streak += 1

            # 조기 종료 (50회 연속 개선 없음)
            if no_improve_streak >= 50 and exp_i > 100:
                if verbose:
                    print(f"\n  수렴 감지 — {exp_i+1}회 실험 후 종료")
                break

        if verbose:
            print(f"\n=== Auto-Research 완료 ===")
            print(f"총 실험: {self.experiment_count}회 | 개선: {len(improvements)}회")
            print(f"최종 점수: {self.best_score:.4f}")

        return self.best_params


# ── 메인 실행 ──────────────────────────────────────────

def run_auto_research(n_experiments: int = 300, verbose: bool = True):
    """전체 auto-research 파이프라인"""

    print("=" * 65)
    print("  TraderScore Auto-Research (Karpathy 방법론)")
    print("=" * 65)
    print()

    # 1. Mainnet 데이터 수집
    print("1. Mainnet 리더보드 수집 중...")
    leaderboard = fetch_leaderboard(100)
    print(f"   → {len(leaderboard)}명 수집")

    # 2. 각 트레이더 positions 수집 (상위 25명)
    print("2. Positions 데이터 수집 중 (상위 25명)...")
    metrics_list = []
    for i, row in enumerate(leaderboard[:25]):
        addr = row.get("address", "")
        try:
            positions = fetch_positions(addr)
            m = compute_metrics(row, positions)
            metrics_list.append(m)
            if verbose:
                print(f"   [{i+1:2d}] {m.alias[:14]:14s} | equity=${m.equity:,.0f} | lev={m.leverage:.1f}x | cons={m.consistency:.2f} | copy={m.copyability:.2f}")
            time.sleep(0.3)
        except Exception as e:
            print(f"   [{i+1}] {addr[:12]} 오류: {e}")

    print(f"   → {len(metrics_list)}명 분석 완료")

    # 3. 기존 페이퍼트레이딩 데이터로 시뮬레이터 구축
    print("\n3. 페이퍼트레이딩 시뮬레이터 준비...")
    sim_data_path = "/root/.openclaw/workspace/paperclip-company/projects/pacifica-hackathon/copy-perp/papertrading/live_result.json"
    closed_trades = []
    if os.path.exists(sim_data_path):
        with open(sim_data_path) as f:
            sim_data = json.load(f)
        closed_trades = sim_data.get("closed_trades", [])
        print(f"   → {len(closed_trades)}건 거래 데이터 로드")
    else:
        print("   ⚠️ live_result.json 없음 — 빈 데이터로 시작")

    sim = FastPaperSim(closed_trades)

    # 4. 초기 파라미터로 기준선 평가
    print("\n4. 초기 기준선 평가...")
    baseline = ScoreParams()
    selected_baseline = select_traders(metrics_list, baseline)
    print(f"   초기 선별: {len(selected_baseline)}명")
    for m, s in selected_baseline:
        print(f"   - {m.alias[:16]:16s} score={s:.3f} | roi30={m.roi_30d:.1f}% | lev={m.leverage:.1f}x | copy={m.copyability:.2f}")

    # 5. Auto-Research 실행
    print(f"\n5. Auto-Research 시작 ({n_experiments}회 실험)...")
    researcher = AutoResearch(metrics_list, sim)
    best_params = researcher.run(n_experiments=n_experiments, verbose=verbose)

    # 6. 최적화 결과 적용
    print("\n6. 최적화된 파라미터로 최종 선별...")
    selected_opt = select_traders(metrics_list, best_params)
    print(f"   최적화 후 선별: {len(selected_opt)}명")
    for m, s in selected_opt:
        print(f"   - {m.alias[:16]:16s} score={s:.3f} | roi30={m.roi_30d:.1f}% | lev={m.leverage:.1f}x | copy={m.copyability:.2f} | mom={m.momentum:.2f}")

    # 7. 결과 저장
    result = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "n_experiments": researcher.experiment_count,
        "improvements": len([h for h in researcher.history if h["improved"]]),
        "best_score": researcher.best_score,
        "best_params": asdict(best_params),
        "selected_traders": [
            {
                "address": m.address,
                "alias": m.alias,
                "score": s,
                "roi_30d": m.roi_30d,
                "leverage": m.leverage,
                "consistency": m.consistency,
                "copyability": m.copyability,
                "momentum": m.momentum,
                "risk_adj_roi": m.risk_adjusted_roi,
                "equity": m.equity,
            }
            for m, s in selected_opt
        ],
        "experiment_history": researcher.history[-50:],  # 마지막 50개만 저장
        "metrics_summary": {
            "total_traders_analyzed": len(metrics_list),
            "traders_passed_filter": len([m for m in metrics_list if score_trader(m, best_params) > 0]),
        }
    }

    out_path = "/root/.openclaw/workspace/paperclip-company/projects/pacifica-hackathon/copy-perp/auto_research_result.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"\n✅ 결과 저장: {out_path}")

    # 8. 파라미터 비교 출력
    print("\n=== 파라미터 Before/After ===")
    baseline_d = asdict(baseline)
    best_d = asdict(best_params)
    for k in baseline_d:
        bv = baseline_d[k]
        av = best_d[k]
        if bv != av:
            print(f"  {k:25s}: {bv} → {av}  {'↑' if av > bv else '↓'}")

    return best_params, selected_opt


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    run_auto_research(n_experiments=n)
