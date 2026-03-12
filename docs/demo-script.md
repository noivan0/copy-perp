# Copy Perp — Demo Script
> Pacifica Hackathon 2026 | Track 3: Social & Gamification
> 10분 데모 시나리오 (심사위원 발표용)

---

## 오프닝 (1분)

**발표자:**
> "CEX에서 카피트레이딩 해보신 분 계세요? eToro, Bybit — 버튼 하나로 고수 따라하는 거요.
> 수백만 명이 쓰는 검증된 수요입니다.
> 근데 퍼프 DEX에는 없습니다. Hyperliquid도, dYdX도, GMX도 없어요.
> **Copy Perp**이 그 공백을 채웁니다 — Pacifica 위에서."

**핵심 메시지 3줄:**
1. 자산은 내 지갑에 — 거래소에 맡기지 않는다
2. 트레이더가 망하면 담보(Performance Bond)가 먼저 깎인다
3. Builder Code로 수수료가 투명하게, 온체인으로

---

## 라이브 데모 (6분)

### Scene 1 — 리더보드 (1분)
- 화면: Copy Perp 프론트엔드 (localhost:8001)
- 보여줄 것: 실시간 티커바 + 트레이더 리더보드
- 멘트: "68개 심볼 실시간 가격. 여기 트레이더들, 7일 ROI랑 승률 다 보입니다."
- 포인트: **BTC 실시간 가격 변동** 눈에 보이게

### Scene 2 — 트레이더 선택 (1분)
- 화면: 리더보드에서 WhaleHunter 클릭 → 팔로우 폼 자동 입력
- 보여줄 것: 비율 설정 (50%), 최대 포지션 한도 ($100)
- 멘트: "한 클릭. 얼마나 따라갈지, 최대 얼마까지 걸지 설정하면 끝."

### Scene 3 — 카피 엔진 (2분)
- 화면: 터미널 + 로그 실시간 출력
- 시나리오:
  1. 트레이더 BTC Long 오픈 (시뮬레이션 이벤트 주입)
  2. Copy Engine 로그: `포지션 변화 감지: BTC open_long Δ0.1`
  3. 팔로워 자동 주문: `[MOCK] BTC bid 0.05 → filled`
- 멘트: "트레이더가 BTC 롱 잡는 순간, 팔로워한테 자동으로 절반 크기 주문 나갑니다."
- 포인트: **속도** — 이벤트 감지에서 주문까지 ms 단위

### Scene 4 — Builder Code + 수수료 (1분)
- 화면: API 요청 payload 보여주기
```json
{
  "symbol": "BTC",
  "side": "bid",
  "amount": "0.05",
  "builder_code": "copyperpv1"
}
```
- 멘트: "팔로워가 거래할 때마다 Builder Code가 붙습니다. 수수료 일부가 플랫폼으로 투명하게 온체인 기록."

### Scene 5 — 레퍼럴 (1분)
- 화면: GET /referral/{address} 응답
- 멘트: "트레이더는 자기 링크 퍼뜨려서 팔로워 모으고, Fuul 포인트 받습니다. 바이럴 성장 구조."

---

## 차별화 포인트 (2분)

| | CEX 카피트레이딩 | Copy Perp |
|---|---|---|
| 자산 | 거래소 보관 | **내 지갑** |
| 트레이더 책임 | 없음 | **Performance Bond** |
| 수수료 | 불투명 | **온체인 Builder Code** |
| 가입 | KYC 필요 | **Google 로그인 30초** |
| 시장 | 현물/선물 | **퍼프 (레버리지)** |

**Performance Bond (로드맵):**
> "트레이더가 팔로워 받으려면 담보 예치합니다. 손실 나면 담보 먼저 깎입니다.
> CEX 카피트레이딩엔 없는 구조예요. 트레이더가 진짜 실력으로 살아남아야 합니다."

---

## 클로징 (1분)

**임팩트:**
- 퍼프 DEX 카피트레이딩 = 미개척 시장
- Pacifica 생태계 거래량 직접 기여 (Builder Code → 모든 복사 거래)
- 트레이더 + 팔로워 + 플랫폼 3자 모두 이득

**로드맵:**
- W1 ✅ 코어 엔진 + API
- W2: 실거래 E2E + Fuul 레퍼럴
- W3: Privy 소셜 로그인
- W4: 스트레스 테스트 + 제출

> "Copy Perp — 고수 따라하기, 이제 온체인으로."

---

## Q&A 예상 질문 & 답변

**Q: 트레이더가 나쁜 거래를 하면?**
A: 팔로워는 언제든 구독 취소 가능. Performance Bond 로드맵으로 트레이더 책임 구조 추가 예정.

**Q: 레이턴시는?**
A: WS account_trades 이벤트 기반 감지 + REST 500ms 폴링 폴백. 실측 3ms 이내 (Mock 모드 10명 동시 복사).

**Q: Pacifica API 어떻게 활용했나?**
A: REST (주문 실행, 포지션 조회) + WS (실시간 가격 68심볼, account_trades 이벤트). Agent Key 서명 방식으로 안전하게.

**Q: 수익 모델은?**
A: Builder Code 수수료 (팔로워 거래량의 일부) + 향후 Performance Bond 예치금 운용.
