# Copy Perp — Project Config
**생성일:** 2026-03-12 | **트랙:** 3 (Social & Gamification)

---

## 계정 정보

| 항목 | 값 |
|------|-----|
| 구글 계정 | nothinkivan@gmail.com |
| 메인 지갑 주소 | `3AHZqrocSguMuo9sUUP8G8YN8NwHwWV2DPUQvbDvtfaQ` |
| 연결 방식 | Privy (구글 소셜 로그인 → 지갑 자동 생성) |
| Agent 주소 | `5RpcRYh1Xw9pMCuAQFdTGhocmeGsEbHg36jFP6nM8DU1` |
| 네트워크 | 테스트넷 (3/16~4/16), 메인넷 (제출 시 전환) |

---

## 테스트넷 엔드포인트

| | URL |
|--|-----|
| REST | `https://test-api.pacifica.fi/api/v1` |
| WebSocket | `wss://test-ws.pacifica.fi/ws` |
| 앱 | `https://test-app.pacifica.fi` |
| Faucet | `https://test-app.pacifica.fi/faucet` |

---

## 보안 규칙

- ⚠️ Private Key는 절대 코드 하드코딩 금지
- `.env` 파일만 사용 (`.gitignore`에 등록됨)
- Agent Key = 서버 서명용 (메인 지갑 키와 별개)

---

## 연동 현황

| 항목 | 상태 | 비고 |
|------|------|------|
| 테스트넷 REST 연결 | ✅ 완료 | 마켓 68개, 체결 이력 확인 |
| Pacifica 클라이언트 코드 | ✅ 완료 | `pacifica/client.py` |
| Agent Wallet 생성 | ✅ 완료 | `.env`에 저장 |
| Agent 바인딩 (앱에서) | ⏳ 대기 | @nothink_ivan 직접 처리 필요 |
| 해커톤 공식 등록 | ✅ 완료 | 구글폼 2026-03-12 제출 완료 |
| Faucet 수령 | ✅ 완료 | 2026-03-12 신청 완료 |
| Builder Code 신청 | ⏳ 대기 | ops@pacifica.fi 또는 Discord |

---

## W1 (3/16~22) 개발 태스크

- [ ] Agent 바인딩 확인
- [x] Faucet에서 테스트 토큰 수령 (2026-03-12 완료)
- [ ] WebSocket account 이벤트 구독 방식 확인
- [ ] FastAPI 라우터 구현 (traders, followers, stats)
- [ ] Copy Engine E2E 테스트 (테스트넷)
- [ ] Builder Code 연동
