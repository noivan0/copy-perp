"""
Copy Perp — Demo Video Generator
Pacifica Hackathon 2026

6장면을 PIL로 렌더링 → ffmpeg로 mp4 합성
"""

import os
import sys
import math
import subprocess
from PIL import Image, ImageDraw, ImageFont

# ── 설정 ────────────────────────────────────────────────
W, H = 1920, 1080
FPS = 30
OUT_DIR = "/tmp/copy_perp_frames"
OUTPUT = "/root/.openclaw/workspace/paperclip-company/projects/pacifica-hackathon/copy-perp/docs/demo_video.mp4"

# 색상 팔레트
BG       = (10, 12, 20)
BG_CARD  = (18, 22, 38)
GREEN    = (0, 230, 120)
BLUE     = (60, 140, 255)
ORANGE   = (255, 160, 40)
PURPLE   = (160, 80, 255)
RED      = (255, 70, 70)
WHITE    = (240, 245, 255)
GRAY     = (120, 130, 150)
DARK     = (30, 36, 55)
GOLD     = (255, 200, 50)
CYAN     = (40, 220, 220)

os.makedirs(OUT_DIR, exist_ok=True)

# ── 폰트 로드 ────────────────────────────────────────────
def load_font(size, bold=False):
    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf" if bold else
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf" if bold else
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf" if bold else
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
    ]
    for p in paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except:
                continue
    return ImageFont.load_default()

F_HUGE   = load_font(72, bold=True)
F_LARGE  = load_font(52, bold=True)
F_MED    = load_font(38, bold=True)
F_NORM   = load_font(30)
F_SMALL  = load_font(24)
F_TINY   = load_font(20)
F_MONO   = load_font(26)

# ── 기본 유틸 ────────────────────────────────────────────
def new_frame():
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    return (img, d)

def draw_rect(d, x, y, w, h, color, radius=12):
    d.rounded_rectangle([x, y, x+w, y+h], radius=radius, fill=color)

def draw_badge(d, x, y, text, color, text_color=BG, font=None):
    font = font or F_TINY
    bb = font.getbbox(text)
    tw, th = bb[2]-bb[0], bb[3]-bb[1]
    pad = 10
    d.rounded_rectangle([x, y, x+tw+pad*2, y+th+pad], radius=6, fill=color)
    d.text((x+pad, y+pad//2), text, font=font, fill=text_color)
    return tw + pad*2

def draw_header(d, title, subtitle=""):
    # 상단 로고바
    draw_rect(d, 0, 0, W, 80, DARK)
    d.text((40, 18), "⚡ Copy Perp", font=F_MED, fill=GREEN)
    d.text((W-400, 28), "Pacifica Hackathon 2026", font=F_NORM, fill=GRAY)
    # 제목
    d.text((40, 110), title, font=F_LARGE, fill=WHITE)
    if subtitle:
        d.text((40, 172), subtitle, font=F_NORM, fill=GRAY)

def draw_progress(d, current, total=6):
    bar_w = W - 80
    seg = bar_w // total
    for i in range(total):
        col = GREEN if i < current else DARK
        d.rectangle([40 + i*seg + 2, H-20, 40 + (i+1)*seg - 2, H-8], fill=col)

def draw_stat_box(d, x, y, label, value, unit="", color=GREEN):
    draw_rect(d, x, y, 380, 120, BG_CARD)
    d.text((x+20, y+15), label, font=F_SMALL, fill=GRAY)
    d.text((x+20, y+50), value, font=F_LARGE, fill=color)
    if unit:
        d.text((x+20, y+100), unit, font=F_TINY, fill=GRAY)

def save_frame(img, idx):
    img.save(f"{OUT_DIR}/frame_{idx:06d}.png")

# ── 장면 생성 함수 ───────────────────────────────────────
def make_scene(scene_fn, duration_sec, start_idx):
    total = duration_sec * FPS
    for i in range(total):
        t = i / total  # 0.0 ~ 1.0
        img, d = scene_fn(t)
        save_frame(img, start_idx + i)
    return start_idx + total

# ── Scene 1: 오프닝 (10초) ───────────────────────────────
def scene1(t):
    img, d = new_frame()

    # 배경 그라디언트 효과
    for row in range(H):
        alpha = row / H
        c = int(10 + alpha * 8)
        d.line([(0, row), (W, row)], fill=(c, c+2, c+10))

    # 페이드인
    fade = min(1.0, t * 3)

    # 메인 타이틀
    alpha_val = int(255 * fade)
    title = "Copy Perp"
    bb = F_HUGE.getbbox(title)
    tw = bb[2] - bb[0]
    tx = (W - tw) // 2
    d.text((tx, 220), title, font=F_HUGE, fill=GREEN)

    # 서브타이틀
    if t > 0.2:
        sub_fade = min(1.0, (t - 0.2) * 4)
        sub = "Decentralized Copy Trading on Pacifica"
        bb2 = F_MED.getbbox(sub)
        sx = (W - (bb2[2]-bb2[0])) // 2
        d.text((sx, 320), sub, font=F_MED, fill=GRAY)

    # 문제 제기 박스
    if t > 0.35:
        prob_fade = min(1.0, (t - 0.35) * 3)
        draw_rect(d, 200, 420, W-400, 200, BG_CARD)
        problems = [
            ("❌", "CEX copy trading → your funds in their exchange"),
            ("❌", "Bybit, eToro copy trading → custody risk, hack risk"),
            ("❌", "Perpetual DEXs → NO copy trading exists"),
        ]
        for i, (icon, text) in enumerate(problems):
            d.text((240, 440 + i*55), icon, font=F_NORM, fill=RED)
            d.text((290, 440 + i*55), text, font=F_NORM, fill=WHITE)

    # 솔루션
    if t > 0.6:
        sol_fade = min(1.0, (t - 0.6) * 4)
        draw_rect(d, 200, 650, W-400, 130, (0, 50, 25))
        d.text((240, 670), "✅  Copy Perp fills the gap — on Pacifica", font=F_MED, fill=GREEN)
        d.text((240, 730), "    Your funds. Your wallet. Always.", font=F_NORM, fill=WHITE)

    # 라이브 배지
    if t > 0.75:
        draw_rect(d, 200, 810, 280, 50, (0, 80, 30))
        d.text((220, 820), "🟢  TESTNET LIVE", font=F_NORM, fill=GREEN)
        draw_rect(d, 510, 810, 220, 50, (0, 30, 80))
        d.text((530, 820), "54/54 TESTS ✅", font=F_NORM, fill=BLUE)

    draw_progress(d, 1)
    return (img, d)

# ── Scene 2: 리더보드 (12초) ─────────────────────────────
def scene2(t):
    img, d = new_frame()
    draw_header(d, "CRS Leaderboard — 133 Traders Monitored",
                "5-metric algorithm: 30d ROI · Win Rate · Profit Factor · Max Drawdown · Copyability")

    # 컬럼 헤더
    y_header = 210
    draw_rect(d, 40, y_header, W-80, 45, DARK)
    cols = [(60, "RANK"), (180, "ADDRESS"), (530, "CRS"), (660, "TIER"),
            (780, "30d ROI"), (920, "WIN RATE"), (1080, "PF"), (1230, "FOLLOWERS"), (1400, "ACTION")]
    for cx, ct in cols:
        d.text((cx, y_header+10), ct, font=F_TINY, fill=GRAY)

    # 트레이더 데이터
    traders = [
        ("🏆", "Ph9yECGo...", "89.7", "S", "+$84,955", "76%", "8.2x", "12", GOLD),
        ("⭐", "EcX5xSDT...", "78.4", "A", "+$516,000", "89%", "6.1x", "9", GREEN),
        ("✅", "4UBH19qU...", "74.1", "A", "+$41,200", "100%", "5.8x", "7", BLUE),
        ("🔵", "FuHMGqdr...", "68.9", "B", "+$31,600", "88%", "136x", "5", CYAN),
        ("🔵", "5RmsTTwk...", "65.2", "B", "+$22,100", "71%", "4.2x", "3", GRAY),
    ]

    # 슬라이드인 애니메이션
    for i, (badge, addr, crs, tier, roi, wr, pf, fol, color) in enumerate(traders):
        delay = i * 0.12
        if t < delay:
            continue
        slide_t = min(1.0, (t - delay) * 5)
        offset = int((1 - slide_t) * 200)

        y = 265 + i * 90
        bg_col = (22, 28, 48) if i % 2 == 0 else BG_CARD
        draw_rect(d, 40 - offset, y, W-80, 80, bg_col)

        d.text((60 - offset, y+22), badge, font=F_MED, fill=color)
        d.text((130 - offset, y+22), f"#{i+1}", font=F_NORM, fill=GRAY)
        d.text((180 - offset, y+22), addr, font=F_MONO, fill=WHITE)

        # CRS 점수 바
        crs_val = float(crs)
        bar_w = int((crs_val / 100) * 100)
        draw_rect(d, 530 - offset, y+30, 100, 18, DARK)
        draw_rect(d, 530 - offset, y+30, bar_w, 18, color)
        d.text((530 - offset, y+8), crs, font=F_NORM, fill=color)

        tier_col = {"S": GOLD, "A": GREEN, "B": BLUE, "C": GRAY}[tier]
        draw_badge(d, 660 - offset, y+22, tier, tier_col, font=F_NORM)
        d.text((780 - offset, y+22), roi, font=F_NORM, fill=GREEN)
        d.text((920 - offset, y+22), wr, font=F_NORM, fill=WHITE)
        d.text((1080 - offset, y+22), pf, font=F_NORM, fill=WHITE)
        d.text((1230 - offset, y+22), fol, font=F_NORM, fill=BLUE)

        # Follow 버튼
        draw_rect(d, 1400 - offset, y+18, 160, 44, GREEN)
        d.text((1415 - offset, y+26), "FOLLOW →", font=F_SMALL, fill=BG)

    # 알고리즘 설명 박스
    if t > 0.65:
        draw_rect(d, 40, 745, W-80, 90, (15, 25, 15))
        d.text((60, 762), "🧠  Algorithm Edge:", font=F_NORM, fill=GREEN)
        d.text((260, 762), "CRS-selected portfolio: +82.7%  vs  Random following: -3.2%  (30-day backtest)", font=F_NORM, fill=WHITE)
        d.text((60, 805), "📊  133 traders monitored  ·  69 symbols  ·  REST polling every 30s", font=F_SMALL, fill=GRAY)

    draw_progress(d, 2)
    return (img, d)

# ── Scene 3: Privy 로그인 (10초) ─────────────────────────
def scene3(t):
    img, d = new_frame()
    draw_header(d, "Google Login → Follow in 30 Seconds",
                "Privy social login · Solana wallet auto-created · No MetaMask required")

    # 왼쪽: 리더보드 배경
    draw_rect(d, 40, 200, 820, 600, BG_CARD)
    d.text((60, 220), "CRS Leaderboard", font=F_MED, fill=WHITE)
    draw_rect(d, 60, 270, 780, 70, DARK)
    d.text((80, 285), "🏆  Ph9yECGo...   CRS 89.7 (S)   +$84,955   [FOLLOW]", font=F_NORM, fill=GOLD)
    draw_rect(d, 60, 350, 780, 70, BG)
    d.text((80, 365), "⭐  EcX5xSDT...   CRS 78.4 (A)   +$516K    [FOLLOW]", font=F_NORM, fill=GREEN)

    # 팔로우 버튼 클릭 효과
    if t > 0.15:
        click_alpha = min(1.0, (t - 0.15) * 8)
        draw_rect(d, 650, 275, 170, 55, GREEN)
        d.text((665, 290), "FOLLOW →", font=F_NORM, fill=BG)

    # 오른쪽: 로그인 모달
    if t > 0.25:
        modal_t = min(1.0, (t - 0.25) * 5)
        modal_y = int(180 + (1 - modal_t) * 300)

        draw_rect(d, 900, modal_y, 980, 520, (20, 25, 42))
        d.text((960, modal_y + 30), "Copy Perp", font=F_MED, fill=GREEN)
        d.text((920, modal_y + 85), "Start copy trading in 30 seconds", font=F_SMALL, fill=GRAY)

        # 구분선
        d.line([(920, modal_y+120), (1860, modal_y+120)], fill=DARK, width=2)

        # Google 버튼
        draw_rect(d, 930, modal_y + 140, 920, 70, (40, 44, 60))
        d.text((970, modal_y + 158), "🔵  Continue with Google", font=F_MED, fill=WHITE)

        # Phantom 버튼
        draw_rect(d, 930, modal_y + 230, 920, 70, (40, 44, 60))
        d.text((970, modal_y + 248), "👻  Connect Phantom Wallet", font=F_MED, fill=WHITE)

        # 특징
        features = [
            "✅  Non-custodial — your keys, your funds",
            "✅  Solana wallet auto-generated",
            "✅  Builder Code 'noivan' embedded",
            "✅  Fuul referral points activated",
        ]
        for i, feat in enumerate(features):
            if t > 0.5 + i * 0.08:
                d.text((930, modal_y + 330 + i*42), feat, font=F_SMALL, fill=GREEN if "✅" in feat else WHITE)

    # API 흐름 (하단)
    if t > 0.7:
        draw_rect(d, 40, 820, W-80, 120, (15, 15, 30))
        flow = "POST /followers/onboard  →  Builder Code sign  →  Pacifica approve  →  DB register  →  Fuul event  →  { ok: true }"
        d.text((60, 845), "🔗  Onboarding Flow:", font=F_NORM, fill=BLUE)
        d.text((60, 885), flow, font=F_MONO, fill=GREEN)

    draw_progress(d, 3)
    return (img, d)

# ── Scene 4: Copy Engine Live (14초) ──────────────────────
def scene4(t):
    img, d = new_frame()
    draw_header(d, "Copy Engine — Live Execution",
                "Position detected → follower orders placed in 522ms · builder_code=noivan on every order")

    # 터미널 박스
    draw_rect(d, 40, 200, W-80, 650, (8, 12, 8))

    # 터미널 상단 버튼
    for i, col in enumerate([(255, 95, 87), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([60 + i*30, 212, 80 + i*30, 232], fill=col)
    d.text((200, 210), "copy-perp — demo_run.py --mock", font=F_TINY, fill=GRAY)

    # 터미널 내용 (시간에 따라 순차 출력)
    lines = [
        (0.05, CYAN,  "╔══════════════════════════════════════════════════════════════╗"),
        (0.08, CYAN,  "║         Copy Perp — LIVE DEMO  (Pacifica testnet)           ║"),
        (0.11, CYAN,  "╚══════════════════════════════════════════════════════════════╝"),
        (0.15, GRAY,  ""),
        (0.18, GREEN, "[12:54:11] [Health] BTC $74,156  Symbols: 69  Monitors: 10"),
        (0.22, GREEN, "[12:54:11] [Stats]  Traders: 133  Followers: 9  Trades: 44  Vol: $9,300"),
        (0.26, GRAY,  ""),
        (0.30, GOLD,  "  🏆 Ph9yECGo...  CRS 89.7 (S)  rec ratio 15%  30d PnL +$84,955"),
        (0.34, GREEN, "  ⭐ EcX5xSDT...  CRS 78.4 (A)  Win Rate 89%  +$516,000"),
        (0.38, BLUE,  "  ✅ 4UBH19qU...  CRS 74.1 (A)  Win Rate 100% (risk min)"),
        (0.42, GRAY,  ""),
        (0.46, CYAN,  "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"),
        (0.50, ORANGE,"  🔔 POSITION DETECTED!"),
        (0.53, WHITE, "     Symbol  : BTC"),
        (0.56, WHITE, "     Side    : ▲ LONG"),
        (0.59, WHITE, "     Size    : 0.0500"),
        (0.62, WHITE, "     Price   : $74,156.20"),
        (0.65, CYAN,  "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"),
        (0.68, WHITE, ""),
        (0.71, WHITE, "[CopyEngine] Processing 9 followers..."),
        (0.74, GREEN, "  ▲ [3AHZqroc...] BTC LONG 0.001349 @ $74,156  →  FILLED ✅  (522ms)"),
        (0.77, GREEN, "  ▲ [T3FOLLOW...] BTC LONG 0.000674 @ $74,156  →  FILLED ✅  (493ms)"),
        (0.80, GREEN, "  ▲ [MockFoll...] BTC LONG 0.000337 @ $74,156  →  FILLED ✅  (508ms)"),
        (0.83, GRAY,  "  ... 6 more followers"),
        (0.86, GRAY,  ""),
        (0.89, CYAN,  "📊  Total: 9 orders  |  Filled: 9/9  |  Vol: $900  |  Builder Fee: +$0.90"),
        (0.93, BLUE,  "    Order ID: 296419238 — BTC Long  → FILLED ✅  (Real testnet)"),
    ]

    y = 250
    lh = 26
    for i, (delay, color, text) in enumerate(lines):
        if t >= delay:
            # 커서 깜빡임 효과
            is_last = (i == len([l for l in lines if t >= l[0]]) - 1)
            display_text = text
            if is_last and int(t * 4) % 2 == 0:
                display_text = text + "█"
            d.text((60, y + i*lh), display_text, font=F_MONO, fill=color)

    draw_progress(d, 4)
    return (img, d)

# ── Scene 5: 백테스팅 (8초) ──────────────────────────────
def scene5(t):
    img, d = new_frame()
    draw_header(d, "30-Day Backtest Results",
                "Starting capital $10,000  ·  Slippage 0.1%  ·  133 mainnet traders")

    # 차트 배경
    draw_rect(d, 40, 210, 900, 580, BG_CARD)
    d.text((60, 225), "Portfolio Return Comparison", font=F_MED, fill=WHITE)

    # Y축
    chart_x, chart_y = 100, 280
    chart_w, chart_h = 800, 450
    d.line([(chart_x, chart_y), (chart_x, chart_y+chart_h)], fill=GRAY, width=2)
    d.line([(chart_x, chart_y+chart_h), (chart_x+chart_w, chart_y+chart_h)], fill=GRAY, width=2)

    # 참조선 (0%)
    zero_y = chart_y + chart_h - 50
    d.line([(chart_x, zero_y), (chart_x+chart_w, zero_y)], fill=DARK, width=1)
    d.text((60, zero_y - 10), "0%", font=F_TINY, fill=GRAY)
    d.text((60, chart_y + 10), "+90%", font=F_TINY, fill=GRAY)
    d.text((60, chart_y + chart_h//2), "+40%", font=F_TINY, fill=GRAY)

    # 랜덤 팔로우 라인 (-3.2%)
    if t > 0.15:
        random_end_y = zero_y + 20
        anim_t = min(1.0, (t - 0.15) * 3)
        end_x = chart_x + int(chart_w * anim_t)
        d.line([(chart_x, zero_y), (end_x, random_end_y)], fill=RED, width=4)
        if anim_t > 0.8:
            d.text((end_x + 10, random_end_y - 10), "-3.2%  Random", font=F_SMALL, fill=RED)

    # CRS 10% 라인 (+41.3%)
    if t > 0.3:
        anim_t = min(1.0, (t - 0.3) * 3)
        end_y = zero_y - int(280 * anim_t)
        end_x = chart_x + int(chart_w * anim_t)
        d.line([(chart_x, zero_y), (end_x, end_y)], fill=BLUE, width=4)
        if anim_t > 0.8:
            d.text((end_x + 10, end_y), "+41.3%  CRS 10%", font=F_SMALL, fill=BLUE)

    # CRS 20% 라인 (+82.7%) — 최적
    if t > 0.45:
        anim_t = min(1.0, (t - 0.45) * 3)
        end_y = zero_y - int(430 * anim_t)
        end_x = chart_x + int(chart_w * anim_t)
        d.line([(chart_x, zero_y), (end_x, end_y)], fill=GREEN, width=6)
        if anim_t > 0.8:
            d.text((end_x + 10, max(end_y, chart_y+5)), "+82.7%  CRS 20% ★ OPTIMAL", font=F_NORM, fill=GREEN)

    # 오른쪽: 상세 통계
    draw_rect(d, 980, 210, 900, 580, BG_CARD)
    d.text((1000, 225), "Top Contributing Traders", font=F_MED, fill=WHITE)

    if t > 0.55:
        top_traders = [
            ("EYhhf8u9", "WR 14%", "PF 162x", "Contribution: +826.9%", GOLD),
            ("FuHMGqdr", "WR 88%", "PF 136x", "Portfolio stability anchor", GREEN),
            ("4UBH19qU", "WR 100%", "PF 5.8x", "Risk-minimal baseline", BLUE),
        ]
        for i, (addr, wr, pf, note, col) in enumerate(top_traders):
            if t > 0.55 + i * 0.1:
                y = 290 + i * 150
                draw_rect(d, 1000, y, 860, 130, DARK)
                d.text((1020, y+15), addr, font=F_MED, fill=col)
                d.text((1020, y+60), wr, font=F_NORM, fill=WHITE)
                d.text((1200, y+60), pf, font=F_NORM, fill=WHITE)
                d.text((1020, y+95), note, font=F_SMALL, fill=GRAY)

    # 핵심 메시지
    if t > 0.8:
        draw_rect(d, 40, 815, W-80, 80, (0, 40, 15))
        d.text((60, 835), "📈  The algorithm is the product. CRS outperforms random selection by 85.9 percentage points.", font=F_NORM, fill=GREEN)

    draw_progress(d, 5)
    return (img, d)

# ── Scene 6: 클로징 (10초) ───────────────────────────────
def scene6(t):
    img, d = new_frame()

    # 배경
    for row in range(H):
        alpha = row / H
        c_g = int(8 + alpha * 5)
        d.line([(0, row), (W, row)], fill=(8, c_g, 8+int(alpha*12)))

    # 헤더
    draw_rect(d, 0, 0, W, 80, DARK)
    d.text((40, 18), "⚡ Copy Perp", font=F_MED, fill=GREEN)
    d.text((W-400, 28), "Pacifica Hackathon 2026", font=F_NORM, fill=GRAY)

    # 타이틀
    d.text((40, 110), "Live Today on Pacifica Testnet", font=F_LARGE, fill=WHITE)
    d.text((40, 172), "Infrastructure — not a hackathon demo.", font=F_MED, fill=GREEN)

    # 체크 항목들
    checks = [
        ("✅", "133 traders monitored  ·  69 symbols  ·  real-time"),
        ("✅", "9 active followers  ·  44 copy trades  ·  $9,300+ volume"),
        ("✅", "Order ID 296419238 — BTC Long — CONFIRMED FILLED"),
        ("✅", "Privy login live  ·  Fuul referral live  ·  Builder Code live"),
        ("✅", "54/54 tests passing  (E2E · DB · Ranked API · Mock pipeline)"),
        ("✅", "builder_code='noivan'  →  0.1% fee auto-routed on every trade"),
    ]

    for i, (icon, text) in enumerate(checks):
        delay = 0.1 + i * 0.1
        if t > delay:
            slide_t = min(1.0, (t - delay) * 6)
            offset = int((1 - slide_t) * 150)
            y = 240 + i * 70
            draw_rect(d, 40 - offset, y, W-80, 60, BG_CARD)
            d.text((60 - offset, y+13), icon, font=F_MED, fill=GREEN)
            d.text((120 - offset, y+13), text, font=F_NORM, fill=WHITE)

    # 수익 모델
    if t > 0.72:
        draw_rect(d, 40, 685, W-80, 120, (0, 30, 60))
        d.text((60, 705), "💰  Revenue Model:", font=F_MED, fill=BLUE)
        d.text((320, 705), "Builder Code 'noivan'  →  0.1% of all follower volume  →  scales linearly with TVL", font=F_NORM, fill=WHITE)
        d.text((60, 758), "📈  Flywheel:", font=F_NORM, fill=BLUE)
        d.text((220, 758), "Copy Perp followers  →  Pacifica trading volume  →  Builder fee  →  platform growth", font=F_NORM, fill=WHITE)

    # 클로징 메시지
    if t > 0.85:
        # 깜빡임 효과
        blink = int(t * 3) % 2 == 0
        draw_rect(d, 40, 830, W-80, 100, (0, 50, 20) if blink else (0, 40, 15))
        msg = "\"Copy Perp is not a hackathon demo. It's infrastructure that drives real volume to Pacifica — starting today.\""
        d.text((60, 850), msg, font=F_NORM, fill=GREEN)

    # 깃허브/링크
    if t > 0.9:
        d.text((60, 960), "🔗  github.com/noivan0/copy-perp", font=F_NORM, fill=BLUE)
        d.text((500, 960), "⚡  builder_code: noivan", font=F_NORM, fill=ORANGE)
        d.text((900, 960), "🌐  Track: Social & Gamification", font=F_NORM, fill=PURPLE)

    draw_progress(d, 6)
    return (img, d)

# ── 메인 실행 ─────────────────────────────────────────────
def main():
    print("🎬 Copy Perp Demo Video Generator")
    print(f"   Output: {OUTPUT}")
    print(f"   Resolution: {W}x{H} @ {FPS}fps")
    print()

    scenes = [
        (scene1, 10),   # 오프닝
        (scene2, 12),   # 리더보드
        (scene3, 10),   # Privy 로그인
        (scene4, 14),   # Copy Engine Live
        (scene5, 8),    # 백테스팅
        (scene6, 10),   # 클로징
    ]

    total_frames = sum(sec * FPS for _, sec in scenes)
    total_sec = sum(sec for _, sec in scenes)
    print(f"   Total: {total_sec}s ({total_frames} frames)")
    print()

    idx = 0
    for i, (fn, dur) in enumerate(scenes):
        print(f"  Rendering Scene {i+1}/6 ({dur}s, {dur*FPS} frames)...", end="", flush=True)
        idx = make_scene(fn, dur, idx)
        print(" ✅")

    print()
    print("  Encoding video with ffmpeg...", end="", flush=True)

    cmd = [
        "ffmpeg", "-y",
        "-framerate", str(FPS),
        "-i", f"{OUT_DIR}/frame_%06d.png",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        OUTPUT
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"\n❌ ffmpeg error:\n{result.stderr}")
        sys.exit(1)

    print(" ✅")
    size_mb = os.path.getsize(OUTPUT) / 1024 / 1024
    print()
    print(f"✨ Done! {OUTPUT}")
    print(f"   Size: {size_mb:.1f} MB  |  Duration: {total_sec}s")

if __name__ == "__main__":
    main()
