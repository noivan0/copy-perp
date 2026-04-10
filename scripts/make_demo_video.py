#!/usr/bin/env python3
"""
Copy Perp — 데모 영상 자동 생성 스크립트
ffmpeg + PIL로 슬라이드 + 터미널 애니메이션 영상 생성
출력: /tmp/copy_perp_demo.mp4 (5분 내외)
"""

import os, sys, math, textwrap, subprocess, shutil, time
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

# ── 설정 ────────────────────────────────────────────────────
W, H = 1280, 720
FPS = 24
OUT_DIR = Path("/tmp/copy_perp_frames")
OUT_VIDEO = Path("/tmp/copy_perp_demo.mp4")

# ── 색상 팔레트 ──────────────────────────────────────────────
BG        = (10, 12, 20)        # 배경 진한 남색
BG2       = (18, 22, 35)        # 패널 배경
ACCENT    = (99, 102, 241)      # 인디고 (Pacifica 스타일)
ACCENT2   = (16, 185, 129)      # 에메랄드 (성공)
ACCENT3   = (245, 158, 11)      # 앰버 (경고/포인트)
WHITE     = (255, 255, 255)
GRAY      = (148, 163, 184)
GRAY2     = (51, 65, 85)
RED       = (239, 68, 68)
GREEN     = (34, 197, 94)
PURPLE    = (168, 85, 247)

# ── 폰트 ────────────────────────────────────────────────────
def load_font(size, bold=False):
    candidates = [
        f"/usr/share/fonts/truetype/dejavu/DejaVuSans{'Bold' if bold else ''}.ttf",
        f"/usr/share/fonts/truetype/liberation/LiberationSans-{'Bold' if bold else 'Regular'}.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()

def load_mono(size):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()

# ── 유틸 ────────────────────────────────────────────────────
def new_frame():
    img = Image.new("RGB", (W, H), BG)
    return img, ImageDraw.Draw(img)

def draw_text_centered(draw, text, y, font, color=WHITE):
    bbox = draw.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    draw.text(((W - w) // 2, y), text, font=font, fill=color)

def draw_rect(draw, x1, y1, x2, y2, fill=None, outline=None, radius=16):
    if fill:
        draw.rounded_rectangle([x1, y1, x2, y2], radius=radius, fill=fill)
    if outline:
        draw.rounded_rectangle([x1, y1, x2, y2], radius=radius, outline=outline, width=2)

def draw_badge(draw, x, y, text, color=ACCENT):
    font = load_font(22, bold=True)
    bbox = draw.textbbox((0, 0), text, font=font)
    pw, ph = 16, 8
    tw, th = bbox[2]-bbox[0], bbox[3]-bbox[1]
    draw_rect(draw, x, y, x+tw+2*pw, y+th+2*ph, fill=color)
    draw.text((x+pw, y+ph), text, font=font, fill=WHITE)
    return x + tw + 2*pw + 12

def draw_progress_bar(draw, x, y, w, h, pct, color=ACCENT):
    draw_rect(draw, x, y, x+w, y+h, fill=GRAY2, radius=h//2)
    draw_rect(draw, x, y, x+int(w*pct), y+h, fill=color, radius=h//2)

def draw_logo_area(draw):
    """상단 로고 바"""
    draw_rect(draw, 0, 0, W, 72, fill=BG2)
    font_logo = load_font(32, bold=True)
    font_sub  = load_font(22)
    draw.text((40, 18), "◈ Copy Perp", font=font_logo, fill=WHITE)
    draw.text((W-340, 24), "Pacifica Hackathon 2026", font=font_sub, fill=GRAY)
    # 구분선
    draw.line([(0, 72), (W, 72)], fill=GRAY2, width=1)

def save_frames(frames_dir, frame_num, img):
    img.save(frames_dir / f"frame_{frame_num:06d}.png")

# ── 프레임 생성기 ────────────────────────────────────────────

def section_title_slide(title, subtitle, duration_s=3):
    """섹션 타이틀 슬라이드"""
    frames = []
    total = int(duration_s * FPS)
    for i in range(total):
        t = i / total
        alpha = min(1.0, t * 3)  # 페이드인

        img, draw = new_frame()
        draw_logo_area(draw)

        # 중앙 그라데이션 박스
        draw_rect(draw, 300, 350, W-300, 730, fill=BG2, radius=24)
        draw_rect(draw, 300, 350, W-300, 730, outline=ACCENT, radius=24)

        fT = load_font(72, bold=True)
        fS = load_font(36)

        # 타이틀
        col = tuple(int(c * alpha) for c in WHITE)
        draw_text_centered(draw, title, 430, fT, col)
        col2 = tuple(int(c * alpha) for c in GRAY)
        draw_text_centered(draw, subtitle, 540, fS, col2)

        frames.append(img)
    return frames


def slide_problem(duration_s=8):
    """슬라이드 1: 문제 제기"""
    frames = []
    total = int(duration_s * FPS)

    problems = [
        ("😰  CEX Copy Trading", "eToro · Bybit · OKX", RED),
        ("🔒  Your funds are held by the exchange", "FTX collapsed → $8B user funds gone", RED),
        ("👁️  Opaque fee structures", "Hidden spreads, undisclosed rebate splits", RED),
        ("🌐  No DeFi composability", "Cannot integrate with on-chain protocols", RED),
    ]

    for i in range(total):
        t = i / total
        img, draw = new_frame()
        draw_logo_area(draw)

        fH = load_font(52, bold=True)
        fS = load_font(28)
        fB = load_font(34, bold=True)

        draw_text_centered(draw, "The Problem with Copy Trading Today", 110, fH, WHITE)

        visible = int(t * (len(problems) + 1))
        for idx, (title, sub, col) in enumerate(problems):
            if idx >= visible:
                break
            y = 230 + idx * 170
            draw_rect(draw, 120, y, W-120, y+140, fill=BG2, radius=16)
            draw_rect(draw, 120, y, W-120, y+140, outline=GRAY2, radius=16)
            draw.text((160, y+20), title, font=fB, fill=col)
            draw.text((160, y+68), sub, font=fS, fill=GRAY)

        frames.append(img)
    return frames


def slide_solution(duration_s=8):
    """슬라이드 2: 솔루션"""
    frames = []
    total = int(duration_s * FPS)

    features = [
        (ACCENT2, "Non-custodial", "Your funds stay in your wallet. Always."),
        (ACCENT,  "CRS Ranking",   "Algorithmic score across 5 metrics — not follower count"),
        (ACCENT3, "30-sec Onboard","Privy Google login → Solana wallet → Follow → Done"),
        (PURPLE,  "Builder Code",  "builder_code=noivan in every order. 0.1% on-chain, transparent"),
    ]

    for i in range(total):
        t = i / total
        img, draw = new_frame()
        draw_logo_area(draw)

        fH = load_font(52, bold=True)
        fB = load_font(34, bold=True)
        fS = load_font(26)

        draw_text_centered(draw, "Copy Perp — Built on Pacifica", 110, fH, WHITE)
        draw_text_centered(draw, "Decentralized copy trading. Non-custodial. Algorithmic.", 178, fS, GRAY)

        visible = int(t * (len(features) + 1))
        cols = 2
        for idx, (col, title, desc) in enumerate(features):
            if idx >= visible: break
            row, c = divmod(idx, cols)
            x = 80 + c * (W//2)
            y = 260 + row * 230
            draw_rect(draw, x, y, x+W//2-100, y+200, fill=BG2, radius=20)
            # 좌측 컬러 바
            draw_rect(draw, x, y, x+8, y+200, fill=col, radius=4)
            draw.text((x+30, y+28), title, font=fB, fill=WHITE)
            # 긴 텍스트 줄바꿈
            wrapped = textwrap.wrap(desc, width=42)
            for j, line in enumerate(wrapped):
                draw.text((x+30, y+78+j*36), line, font=fS, fill=GRAY)

        frames.append(img)
    return frames


def slide_leaderboard(duration_s=10):
    """슬라이드 3: CRS 리더보드 시연"""
    frames = []
    total = int(duration_s * FPS)

    traders = [
        ("EcX5xSDT...", "S", "+82.5%", "74%",  "0.34", "$145K", ACCENT2),
        ("4UBH19qU...", "S", "+58.4%", "100%", "0.28", "$89K",  ACCENT),
        ("A6VY4ZBU...", "A", "+58.9%", "49%",  "0.61", "$67K",  ACCENT3),
        ("HtC4WT6J...", "A", "+43.1%", "68%",  "0.19", "$52K",  ACCENT),
        ("YjCD9Gek...", "B", "+31.7%", "55%",  "0.42", "$38K",  GRAY),
    ]
    headers = ["Trader", "Tier", "30d ROI", "Win Rate", "Max DD", "Volume", ""]

    for i in range(total):
        t = i / total
        img, draw = new_frame()
        draw_logo_area(draw)

        fH = load_font(44, bold=True)
        fT = load_font(24, bold=True)
        fS = load_font(22)
        fB = load_font(26, bold=True)

        draw_text_centered(draw, "CRS Leaderboard — Top Performers", 100, fH, WHITE)
        draw_text_centered(draw, "Copy Reliability Score: Momentum 30% · Profitability 25% · Risk 20% · Consistency 15% · Copyability 10%", 158, load_font(20), GRAY)

        # 테이블 헤더
        cols_x = [80, 300, 460, 620, 800, 960, 1160]
        y_hdr = 210
        draw_rect(draw, 60, y_hdr-10, W-60, y_hdr+44, fill=GRAY2, radius=8)
        for ci, hdr in enumerate(headers):
            draw.text((cols_x[ci], y_hdr+4), hdr, font=fT, fill=GRAY)

        # 행
        visible_rows = min(len(traders), int(t * (len(traders)+1)))
        for ri, (addr, tier, roi, wr, dd, vol, col) in enumerate(traders[:visible_rows]):
            y = 270 + ri * 120
            # 행 배경
            bg = BG2 if ri % 2 == 0 else (22, 28, 42)
            draw_rect(draw, 60, y-8, W-60, y+96, fill=bg, radius=12)

            # 티어 배지
            tier_col = {
                "S": ACCENT2, "A": ACCENT, "B": ACCENT3, "C": GRAY
            }.get(tier, GRAY)

            draw.text((cols_x[0], y+10), addr, font=fS, fill=WHITE)
            draw_badge(draw, cols_x[1], y+8, f" {tier} ", tier_col)
            draw.text((cols_x[2], y+10), roi, font=fB, fill=ACCENT2)
            draw.text((cols_x[3], y+10), wr,  font=fS, fill=WHITE)
            draw.text((cols_x[4], y+10), dd,  font=fS, fill=ACCENT3)
            draw.text((cols_x[5], y+10), vol, font=fS, fill=WHITE)

            # Follow 버튼
            if ri < 2:
                draw_rect(draw, cols_x[6], y+8, cols_x[6]+160, y+60, fill=ACCENT, radius=12)
                draw.text((cols_x[6]+20, y+18), "Follow →", font=fT, fill=WHITE)

        frames.append(img)
    return frames


def slide_onboarding(duration_s=10):
    """슬라이드 4: 30초 온보딩 플로우"""
    frames = []
    total = int(duration_s * FPS)

    steps = [
        (ACCENT,  "1", "Click 'Connect Wallet'",       "No MetaMask. No seed phrase."),
        (ACCENT2, "2", "Google / Discord Login",        "Privy creates Solana wallet automatically"),
        (ACCENT3, "3", "Select Trader (CRS S-Tier)",    "EcX5xSDT... +82.5% ROI — Follow"),
        (PURPLE,  "4", "Set copy_ratio = 10%, Max $50", "Proportional position sizing"),
        (GREEN,   "5", "Done. Copy Engine Active.",     "Next trade → yours executes in <600ms"),
    ]

    for i in range(total):
        t = i / total
        img, draw = new_frame()
        draw_logo_area(draw)

        fH = load_font(48, bold=True)
        fB = load_font(32, bold=True)
        fS = load_font(26)
        fN = load_font(52, bold=True)

        draw_text_centered(draw, "30-Second Onboarding", 100, fH, WHITE)
        draw_text_centered(draw, "From zero to copy trading in under a minute.", 162, fS, GRAY)

        visible = int(t * (len(steps)+1))
        for idx, (col, num, title, desc) in enumerate(steps):
            if idx >= visible: break
            y = 230 + idx * 148

            # 번호 원
            draw.ellipse([60, y, 120, y+60], fill=col)
            draw.text((77 if len(num)==1 else 68, y+7), num, font=fN, fill=WHITE)

            draw_rect(draw, 140, y-4, W-80, y+100, fill=BG2, radius=14)
            draw.text((172, y+8),  title, font=fB, fill=WHITE)
            draw.text((172, y+52), desc,  font=fS, fill=GRAY)

        # 타이머 표시
        elapsed = t * 30
        timer_color = ACCENT2 if elapsed < 28 else ACCENT3
        fT2 = load_font(32, bold=True)
        draw.text((W-240, 90), f"⏱ {elapsed:.1f}s", font=fT2, fill=timer_color)

        frames.append(img)
    return frames


def slide_copy_engine(duration_s=14):
    """슬라이드 5: Copy Engine 실시간 시연 (터미널 애니메이션)"""
    frames = []
    total = int(duration_s * FPS)

    terminal_lines = [
        (0.05, "GRAY",   "🔄 Copy Engine v2.1 — Monitoring 133 traders, 69 symbols"),
        (0.10, "GRAY",   "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"),
        (0.15, "GRAY",   "[04:10:22] Polling cycle #1,847 ..."),
        (0.22, "ACCENT3","🔔  POSITION DETECTED!"),
        (0.24, "WHITE",  "   Trader:   EcX5xSDT... (CRS S-Tier, +82.5% 30d)"),
        (0.27, "WHITE",  "   Market:   BTC-USD-PERP"),
        (0.29, "WHITE",  "   Side:     LONG  ↑"),
        (0.31, "WHITE",  "   Size:     0.0500 BTC @ $72,796"),
        (0.34, "ACCENT", "⚙️  CopyEngine — Computing follower orders..."),
        (0.37, "GRAY",   "   Followers: 12 active | copy_ratio: 5–20%"),
        (0.40, "GRAY",   "   Builder Code: noivan (fee: 0.1%)"),
        (0.44, "GRAY",   "   ─────────────────────────────────"),
        (0.48, "ACCENT2","   ▲ [3AHZqroc...] BTC LONG  0.000688 BTC → FILLED ✅  522ms"),
        (0.52, "ACCENT2","   ▲ [Follower_B] BTC LONG  0.001377 BTC → FILLED ✅  453ms"),
        (0.56, "ACCENT2","   ▲ [Follower_C] BTC LONG  0.000344 BTC → FILLED ✅  489ms"),
        (0.60, "ACCENT2","   ▲ [Follower_D] BTC LONG  0.000688 BTC → FILLED ✅  511ms"),
        (0.64, "ACCENT2","   ▲ [Follower_E] BTC LONG  0.000500 BTC → FILLED ✅  478ms"),
        (0.68, "ACCENT2","   ▲ [Follower_F] BTC LONG  0.000750 BTC → FILLED ✅  534ms"),
        (0.72, "GRAY",   "   ─────────────────────────────────"),
        (0.76, "WHITE",  "📊  Summary: 6/6 orders FILLED"),
        (0.79, "WHITE",  "   Total volume: $450.23"),
        (0.82, "ACCENT3","   Builder fee: +$0.45 (on-chain, builder_code=noivan)"),
        (0.86, "ACCENT2","   Avg latency: 498ms  ⚡"),
        (0.90, "GRAY",   "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"),
        (0.93, "GRAY",   "[04:10:23] Next cycle in 28s ..."),
    ]

    color_map = {
        "GRAY": GRAY, "WHITE": WHITE, "ACCENT": ACCENT,
        "ACCENT2": ACCENT2, "ACCENT3": ACCENT3
    }

    for i in range(total):
        t = i / total
        img, draw = new_frame()
        draw_logo_area(draw)

        fH = load_font(44, bold=True)
        fC = load_mono(22)
        fS = load_font(26)

        draw_text_centered(draw, "Copy Engine — Live Order Replication", 100, fH, WHITE)

        # 터미널 창
        tx, ty, tw, th = 60, 160, W-120, H-80
        draw_rect(draw, tx, ty, tx+tw, ty+th, fill=(8, 10, 16), radius=16)
        draw_rect(draw, tx, ty, tx+tw, ty+th, outline=GRAY2, radius=16)

        # 터미널 타이틀바
        draw_rect(draw, tx, ty, tx+tw, ty+40, fill=(28, 32, 48), radius=16)
        for bi, bc in enumerate([RED, ACCENT3, ACCENT2]):
            draw.ellipse([tx+20+bi*24, ty+12, tx+34+bi*24, ty+26], fill=bc)
        draw.text((tx+80, ty+10), "copy_engine — bash", font=load_font(20), fill=GRAY)

        # 라인 출력
        visible_lines = [(trig, col, txt) for trig, col, txt in terminal_lines if t >= trig]
        for li, (_, col, txt) in enumerate(visible_lines):
            if li >= 22: break
            draw.text((tx+20, ty+54+li*34), txt, font=fC, fill=color_map.get(col, WHITE))

        frames.append(img)
    return frames


def slide_backtest(duration_s=8):
    """슬라이드 6: 백테스팅 결과"""
    frames = []
    total = int(duration_s * FPS)

    for i in range(total):
        t = i / total
        img, draw = new_frame()
        draw_logo_area(draw)

        fH = load_font(48, bold=True)
        fB = load_font(36, bold=True)
        fS = load_font(26)
        fBig = load_font(96, bold=True)

        draw_text_centered(draw, "Backtesting Results — 30 Days Pacifica Mainnet", 100, fH, WHITE)

        # 두 박스 비교
        # CRS 선택
        draw_rect(draw, 80, 190, 900, 820, fill=BG2, radius=20)
        draw_rect(draw, 80, 190, 900, 820, outline=ACCENT2, radius=20)
        draw.text((200, 220), "CRS-Selected Portfolio", font=fB, fill=ACCENT2)
        draw.text((200, 278), "copy_ratio = 20%", font=fS, fill=GRAY)

        roi_val = min(82.7, 82.7 * min(1, (t-0.1)/0.5)) if t > 0.1 else 0
        draw.text((200, 360), f"+{roi_val:.1f}%", font=fBig, fill=ACCENT2)
        draw.text((200, 490), "30-day ROI", font=fS, fill=GRAY)

        metrics_crs = [
            ("Sharpe Ratio",  "1.84"),
            ("Win Rate",      "74.3%"),
            ("Max Drawdown",  "8.2%"),
            ("Avg Trade",     "+1.1%"),
        ]
        for mi, (k, v) in enumerate(metrics_crs):
            draw.text((200, 560+mi*56), k, font=fS, fill=GRAY)
            draw.text((520, 560+mi*56), v, font=load_font(26, True), fill=WHITE)

        # VS
        draw_text_centered(draw, "VS", 500, load_font(52, bold=True), GRAY)

        # 무작위 랜덤
        draw_rect(draw, 1020, 190, W-80, 820, fill=BG2, radius=20)
        draw_rect(draw, 1020, 190, W-80, 820, outline=RED, radius=20)
        draw.text((1100, 220), "Random Following", font=fB, fill=RED)
        draw.text((1100, 278), "No filtering", font=fS, fill=GRAY)

        roi_rnd = max(-3.2, -3.2 * min(1, (t-0.15)/0.5)) if t > 0.15 else 0
        draw.text((1100, 360), f"{roi_rnd:.1f}%", font=fBig, fill=RED)
        draw.text((1100, 490), "30-day ROI", font=fS, fill=GRAY)

        metrics_rnd = [
            ("Sharpe Ratio",  "−0.12"),
            ("Win Rate",      "41.2%"),
            ("Max Drawdown",  "31.8%"),
            ("Avg Trade",     "−0.08%"),
        ]
        for mi, (k, v) in enumerate(metrics_rnd):
            draw.text((1100, 560+mi*56), k, font=fS, fill=GRAY)
            draw.text((1430, 560+mi*56), v, font=load_font(26, True), fill=WHITE)

        frames.append(img)
    return frames


def slide_closing(duration_s=8):
    """슬라이드 7: 클로징"""
    frames = []
    total = int(duration_s * FPS)

    for i in range(total):
        t = i / total
        img, draw = new_frame()
        draw_logo_area(draw)

        fH = load_font(72, bold=True)
        fS = load_font(30)
        fB = load_font(34, bold=True)

        draw_text_centered(draw, "Copy Perp", 200, fH, WHITE)
        draw_text_centered(draw, "The first non-custodial copy trading layer on Pacifica", 310, fS, GRAY)

        items = [
            (ACCENT2, "✅  Non-custodial — your keys, your funds"),
            (ACCENT,  "✅  CRS algorithmic ranking — +82.7% backtest"),
            (ACCENT3, "✅  Builder Code noivan — on-chain revenue"),
            (PURPLE,  "✅  30-second onboarding via Privy"),
            (GREEN,   "✅  69 PASS test suite — production ready"),
        ]
        for idx, (col, txt) in enumerate(items):
            alpha = min(1.0, max(0, (t - idx*0.1) * 5))
            c = tuple(int(a*alpha) for a in col)
            draw.text((300, 400 + idx*90), txt, font=fB, fill=c)

        if t > 0.6:
            draw_text_centered(draw, "github.com/noivan0/copy-perp", 870, fS, ACCENT)
            draw_text_centered(draw, "Pacifica Hackathon 2026  |  Builder Code: noivan", 920, load_font(24), GRAY)

        frames.append(img)
    return frames


# ── 메인 ────────────────────────────────────────────────────

def main():
    print("🎬 Copy Perp 데모 영상 생성 시작")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # 이전 프레임 정리
    for f in OUT_DIR.glob("*.png"):
        f.unlink()

    print("📸 프레임 렌더링 중...")
    frame_idx = 0

    sections = [
        ("인트로",           section_title_slide("Copy Perp", "Decentralized Copy Trading on Pacifica", 5)),
        ("문제 제기",         slide_problem(25)),
        ("솔루션",           slide_solution(25)),
        ("CRS 리더보드",     slide_leaderboard(35)),
        ("온보딩",           slide_onboarding(35)),
        ("Copy Engine",     slide_copy_engine(45)),
        ("백테스팅",         slide_backtest(30)),
        ("클로징",           slide_closing(25)),
    ]

    total_frames = sum(len(fr) for _, fr in sections)
    done = 0

    for name, frames in sections:
        print(f"  [{name}] {len(frames)} 프레임")
        for img in frames:
            save_frames(OUT_DIR, frame_idx, img)
            frame_idx += 1
            done += 1
            if done % 100 == 0:
                print(f"  진행: {done}/{total_frames} ({done*100//total_frames}%)")

    print(f"\n✅ 총 {frame_idx} 프레임 저장 완료")
    print(f"🎥 ffmpeg 인코딩 시작...")

    cmd = [
        "ffmpeg", "-y",
        "-framerate", str(FPS),
        "-i", str(OUT_DIR / "frame_%06d.png"),
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(OUT_VIDEO)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("❌ ffmpeg 오류:", result.stderr[-500:])
        sys.exit(1)

    size_mb = OUT_VIDEO.stat().st_size / 1024 / 1024
    duration_s = frame_idx / FPS
    print(f"\n🏁 완료!")
    print(f"   출력: {OUT_VIDEO}")
    print(f"   크기: {size_mb:.1f} MB")
    print(f"   길이: {duration_s:.0f}초 ({duration_s/60:.1f}분)")

    # 정리
    shutil.rmtree(OUT_DIR)
    print("   프레임 임시파일 정리 완료")

if __name__ == "__main__":
    main()
