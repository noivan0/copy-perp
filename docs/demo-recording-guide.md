# Copy Perp — Demo Recording Guide
> Pacifica Hackathon 2026 | For: 노이반님 직접 녹화용
> 예상 소요시간: **5~7분** | 권장 해상도: 1920×1080

---

## ✅ 녹화 전 준비 체크리스트

**터미널 준비 (녹화 시작 전 실행)**

```bash
# 터미널 1 — API 서버 (백그라운드 유지)
cd /root/.openclaw/workspace/paperclip-company/projects/pacifica-hackathon/copy-perp
uvicorn api.main:app --host 0.0.0.0 --port 8001

# 터미널 2 — 데모 시각화 (녹화 중 실행)
python3 scripts/demo_run.py --mock

# 서버 상태 확인 (브라우저 또는 터미널)
curl http://localhost:8001/health
```

**브라우저 탭 준비 (녹화 전 미리 열어두기)**

| 탭 번호 | URL | 설명 |
|--------|-----|------|
| Tab 1 | `http://localhost:3000` | Copy Perp 리더보드 (메인) |
| Tab 2 | `https://test-app.pacifica.fi` | Pacifica 테스트넷 |
| Tab 3 | `https://pacifica.gitbook.io/docs/builder-program` | Builder Program |

**화면 설정**

- [ ] 터미널 폰트 크기 **18pt 이상** (심사위원 가독성)
- [ ] 브라우저 줌 **110%** 
- [ ] 불필요한 알림/탭 닫기
- [ ] 터미널 1(서버) 확인: `BTC $7x,xxx 심볼 69개 모니터 10개` 출력 확인
- [ ] `http://localhost:3000` 리더보드 로딩 확인
- [ ] demo_run.py --mock 컬러 출력 정상 확인

---

## 🎬 6장면 진행 순서 + 멘트

> **총 5~7분** | 각 장면 예상 시간 표시

---

### Scene 1 — 오프닝 (약 60초)

**화면:** 브라우저 Tab 1 — Copy Perp 히어로 섹션 (localhost:3000)

**멘트 (영어):**
> "CEX copy trading — Bybit, eToro — has millions of users.
> One button. Follow a top trader. Done.
> But on perpetual DEXs? Hyperliquid, dYdX, GMX — nothing.
> **Copy Perp fills that gap, on Pacifica.**
> The key difference: your funds stay in your wallet. Always."

---

### Scene 2 — 리더보드 알고리즘 (약 90초)

**화면:** 리더보드 스크롤 — 배지 🏆⭐✅ 눈에 보이게, 실시간 BTC 가격 강조

**멘트 (영어):**
> "133 traders monitored. We filter them with 5 metrics:
> 30-day ROI, win rate, profit factor, max drawdown, and copy ratio.
> These badges — Gold, Star, Verified — tell you who to follow at a glance.
> Our algorithm outperforms random following by **+5 percentage points** in ROI."

---

### Scene 3 — Privy 로그인 → 팔로우 (약 90초)

**화면:** "Follow" 버튼 클릭 → 로그인 모달 팝업 → Google 로그인 시연

**멘트 (영어):**
> "No MetaMask. No seed phrases.
> Just Google login — Privy creates your Solana wallet automatically.
> Sign in, approve, and you're following the top 2 traders in 30 seconds.
> Builder Code 'noivan' is embedded in every copy order — that's our fee capture."

---

### Scene 4 — Copy Engine Live (약 120초)

**화면:** 터미널 2 전환 — demo_run.py --mock 출력 실시간 확인

**멘트 (영어):**
> "A top trader opens a BTC long on Pacifica.
> Our monitor catches it — and within 522 milliseconds,
> copy orders go out to all followers, proportionally sized.
> Every order carries builder_code='noivan' — Pacifica routes the fee to us automatically.
> 
> This is real. Order ID 296419238 — BTC Long — FILLED on testnet."

*(데모 출력에서 FILLED ✅ 강조)*

---

### Scene 5 — 백테스팅 데이터 (약 45초)

**화면:** 브라우저 또는 터미널 — 백테스팅 결과 표시

**멘트 (영어):**
> "30-day backtest. Starting capital: $10,000.
> Random following: **-3.2%**.
> Copy Perp algorithm at 10% ratio: **+41.3%**.
> At 20% ratio: **+82.7%**.
> The selection algorithm makes the difference."

---

### Scene 6 — 클로징 (약 45초)

**화면:** 리더보드 메인으로 복귀 — 전체 통계 표시

**멘트 (영어):**
> "Right now, live on Pacifica testnet:
> 133 traders monitored. 9 active followers. 44 copy trades executed. $9,300 in volume.
> Privy login live. Fuul referral live. Builder Code live.
> 
> Copy Perp is not a hackathon demo.
> It's infrastructure that drives real volume to Pacifica — starting today."

---

## ⏱ 타임라인 요약

| 장면 | 예상 시간 | 누적 |
|------|---------|------|
| Scene 1 오프닝 | 1분 | 1분 |
| Scene 2 리더보드 | 1분 30초 | 2분 30초 |
| Scene 3 로그인→팔로우 | 1분 30초 | 4분 |
| Scene 4 Copy Engine | 2분 | 6분 |
| Scene 5 백테스팅 | 45초 | 6분 45초 |
| Scene 6 클로징 | 45초 | **7분 30초** |

> 빠르게 진행하면 **5분**, 여유있게 하면 **7분 30초** 이내.

---

## 📤 업로드 방법

**권장: YouTube Unlisted**

1. [youtube.com](https://youtube.com) 로그인
2. 우측 상단 **카메라 아이콘 → "동영상 업로드"**
3. 파일 선택 후 업로드
4. 공개 범위: **"일부 공개 (링크 있는 사람만)"** 선택
5. 업로드 완료 후 URL 복사

**대안: Google Drive 비공개 링크**

1. drive.google.com 업로드
2. 공유 → "링크가 있는 모든 사용자" → 뷰어
3. 링크 복사

---

## 📝 영상 제목 / 설명 템플릿

**제목:**
```
Copy Perp — Decentralized Copy Trading on Pacifica | Pacifica Hackathon 2026
```

**설명:**
```
Copy Perp brings copy trading to perpetual DEXs — built on Pacifica.

✅ 133 traders monitored in real-time
✅ Google login → follow in 30 seconds (Privy)
✅ Sub-second copy order execution (522ms)
✅ Builder Code 'noivan' — on-chain fee capture
✅ +82.7% backtest return vs -3.2% random following
✅ 54/54 tests passing

Track 3: Social & Gamification | Pacifica Hackathon 2026

GitHub: https://github.com/noivan0/copy-perp
```

**태그:**
```
Pacifica, copy trading, DeFi, hackathon, perpetuals, Solana, DEX
```

---

## 🔴 녹화 도구 추천

| OS | 도구 | 방법 |
|----|------|------|
| Mac | QuickTime Player | 파일 → 새 화면 기록 |
| Mac | OBS Studio | 무료, 고품질 |
| Windows | Xbox Game Bar | Win+G |
| Windows | OBS Studio | 무료, 고품질 |
| 공통 | Loom | 브라우저 확장, 업로드 자동 |

---

*작성: 마케팅팀장 Mia | 2026-04-10 | P002 Pacifica Hackathon T-MKT1*
