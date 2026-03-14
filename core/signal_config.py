"""
signal_config.py — CRS 실험 파라미터 중앙 관리
AutoResearch 실험 시 이 파일만 수정하여 A/B 테스트.
"""

# ── CRS 가중치 (합 = 1.0) ─────────────────────────────────────
W_MOMENTUM    = 0.35   # 7d/30d momentum ratio
W_PROFIT      = 0.30   # ROI 기반 수익성
W_RISK        = 0.20   # OI/Equity 리스크 (낮을수록 좋음)
W_CONSISTENCY = 0.15   # 4주 연속 플러스 일관성

# ── Hard Filter 임계값 ────────────────────────────────────────
MIN_MOM_RATIO    = 0.05   # 최소 7d/30d momentum ratio (음수 제거)
MAX_OI_RATIO     = 3.0    # 최대 OI/Equity (레버리지 한도)
MIN_CONSISTENCY  = 3      # 최소 consistency score (5점 만점)
MIN_ROI_30D      = 0.03   # 최소 30일 ROI 3%
MAX_ROI_30D      = 2.00   # 최대 30일 ROI 200% (이상치 제거)
MIN_7D_PNL       = -500   # 최근 7일 최소 PnL (급락 제거)
MIN_ACTIVE_DAYS  = 7      # 최소 활성 일수

# ── Tier 기준 ─────────────────────────────────────────────────
TIER_S_MIN_CRS   = 80
TIER_A_MIN_CRS   = 65
TIER_B_MIN_CRS   = 50

# ── 복사 비율 ─────────────────────────────────────────────────
TIER_S_COPY = 0.20
TIER_A_COPY = 0.15
TIER_B_COPY = 0.10

# ── 점수 공식 파라미터 ────────────────────────────────────────
# Momentum 점수: sigmoid 형태로 포화
MOM_SIGMOID_K    = 5.0    # sigmoid steepness
MOM_SIGMOID_X0   = 0.2    # inflection point (20% momentum)

# OI/Equity 패널티: 선형 vs 비선형
OI_PENALTY_LINEAR   = False  # True=선형, False=비선형(제곱)
OI_SOFT_LIMIT       = 1.0   # soft limit (이하면 패널티 없음)

# ROI 점수: log 스케일 보정
ROI_LOG_SCALE    = True    # log(1+roi) 사용 여부
ROI_SATURATION   = 0.50   # 50% 이상은 포화 (이상치 방지)

# ── 실험 메타 ─────────────────────────────────────────────────
EXPERIMENT_TAG   = "baseline"
EXPERIMENT_NOTE  = "기본 설정 — autoresearch round 1 시작점"
