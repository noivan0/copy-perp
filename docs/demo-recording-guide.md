# Copy Perp — 데모 영상 촬영 가이드
> 작성: Dev팀장 | 2026-04-10 | 촬영 목표일: 2026-04-14

---

## 📋 사전 준비 (촬영 전날)

### 환경 체크
```bash
# 1. 백엔드 기동 확인
cd copy-perp
uvicorn api.main:app --host 0.0.0.0 --port 8001
curl http://localhost:8001/health | python3 -m json.tool
# → "status": "ok", "btc_mark": "7x,xxx" 확인

# 2. 프론트엔드 기동 확인
cd copy-perp-web
npm run dev
# → http://localhost:3000 접속

# 3. 데모 스크립트 리허설
cd copy-perp
python3 scripts/demo_run.py --mock
# → 컬러 터미널 출력 확인

# 4. Builder Code 상태 확인
python3 scripts/verify_builder_code.py
```

### 터미널 설정
- 폰트 크기: **18pt 이상** (심사위원 가독성)
- 배경: 검정 or 다크 테마
- 터미널 창: 전체화면

---

## 🎬 촬영 구성 (10분 영상)

### [00:00-01:00] 오프닝
**화면:** 브라우저 → http://localhost:3000
**멘트:**
> "Pacifica DEX에서 카피트레이딩이 가능합니다.
> 30초 만에 최고 트레이더를 따라갈 수 있습니다."

### [01:00-03:00] 리더보드 & CRS 랭킹
**화면:** 브라우저 → CRS 랭킹 섹션
**멘트:**
> "109명 중 CRS 알고리즘이 선별한 S/A 등급 트레이더.
> 백테스팅 결과 무작위 대비 +5%p ROI 차이."

### [03:00-05:00] Privy 로그인 → 팔로우
**화면:** "Connect Wallet" 클릭 → Google 로그인 → 팔로우 버튼
**멘트:**
> "MetaMask 불필요. Google 계정으로 Solana 지갑 자동 생성.
> 팔로우 버튼 클릭 → copy_ratio 10% → 최대 $50 설정."

### [05:00-08:00] Copy Engine 라이브 시연
**화면:** 새 터미널 → `python3 scripts/demo_run.py --mock`

```
[보여줄 출력]
🔔 포지션 감지!  BTC LONG ▲0.0500 @ $72,796
[CopyEngine] 팔로워 12명 대상 주문 계산 중...
  ▲ [3AHZqroc...] BTC LONG 0.000688 → FILLED ✅ (522ms)
  ▲ [Follower_B] BTC LONG 0.001377 → FILLED ✅ (453ms)
📊 총 6건 | 체결 6/6 | 거래량 $450 | Builder Fee +$0.45
```

**멘트:**
> "트레이더 주문 → 522ms → 팔로워 비례 주문 체결.
> 모든 주문에 Builder Code 'noivan' 자동 포함."

### [08:00-09:00] 백테스팅 증거
**화면:** 터미널 → API 응답
```bash
curl http://localhost:8001/portfolio/backtest | python3 -m json.tool
```
**멘트:**
> "30일 백테스팅: ratio=20% → +82.7% ROI.
> 무작위 팔로우 대비 +85.9%p 차이."

### [09:00-10:00] 클로징
**화면:** README.md → Submission Checklist
**멘트:**
> "GitHub 공개, Testnet 실거래 확인, Builder Code 'noivan'.
> Copy Perp — Pacifica 위의 첫 번째 진정한 카피트레이딩."

---

## 🔴 촬영 시 주의사항

1. **AGENT_PRIVATE_KEY 화면 노출 금지** — `.env` 파일 절대 표시 금지
2. **Mock 모드 사용** — 실제 주문 X, `--mock` 플래그 사용
3. **BTC 가격 표시 확인** — `/health` 응답에 btc_mark 있어야 함

---

## 📤 YouTube 업로드

1. 영상 제목: `Copy Perp — Decentralized Copy Trading on Pacifica | Hackathon Demo 2026`
2. 설명: README.md 내용 요약 + GitHub 링크
3. 태그: `pacifica`, `defi`, `copy-trading`, `solana`, `hackathon`
4. 공개 설정: **Public**
5. URL → README.md Line 1 "Watch Demo" 링크 교체
6. URL → 제출 폼에 붙여넣기

---

## ✅ 촬영 후 체크

```
□ 영상 10분 이내
□ 음성 선명
□ 코드/터미널 가독성 (18pt+)
□ private key 노출 없음
□ YouTube Public 설정
□ README.md 링크 업데이트
□ git push
□ 제출 폼 링크 삽입
```
