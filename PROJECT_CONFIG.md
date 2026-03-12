# Copy Perp — 프로젝트 설정
**업데이트:** 2026-03-12

---

## 계정 정보

| 항목 | 값 |
|---|---|
| **구글 계정** | nothinkivan@gmail.com |
| **Pacifica 테스트넷 지갑 주소** | `3AHZqrocSguMuo9sUUP8G8YN8NwHwWV2DPUQvbDvtfaQ` |
| **연동 방식** | 구글 소셜 로그인 (Privy 지갑) |
| **테스트넷 URL** | https://test-app.pacifica.fi |

---

## 즉시 처리 필요 (개발팀)

### 1. 해커톤 공식 등록
- URL: https://forms.gle/1FP2EuvZqYiP7Tiy7
- **@nothink_ivan 직접 등록**

### 2. Builder Code 신청
- 이메일: ops@pacifica.fi
- Discord: #builder-program
- 신청 시 위 지갑 주소 사용: `3AHZqrocSguMuo9sUUP8G8YN8NwHwWV2DPUQvbDvtfaQ`
- **3/16 개발 시작 전 신청 권장**

### 3. Faucet 테스트 토큰 수령
- URL: https://test-app.pacifica.fi/faucet
- 위 지갑에 테스트 USDC 수령

---

## SDK 연동 설정 (개발팀 참고)

```python
# pacifica/client.py 설정
TESTNET_REST = "https://test-api.pacifica.fi/api/v1"
TESTNET_WS   = "wss://test-ws.pacifica.fi/ws"

# 트레이더 (Leader) 지갑
LEADER_ADDRESS = "3AHZqrocSguMuo9sUUP8G8YN8NwHwWV2DPUQvbDvtfaQ"

# 주의: Private Key는 절대 여기 저장 금지 → .env 파일만 사용
# LEADER_PRIVATE_KEY = os.getenv("LEADER_PRIVATE_KEY")
```

---

## 상태

- [x] Pacifica 테스트넷 접속 완료 (구글 로그인)
- [x] 지갑 주소 확인: `3AHZqrocSguMuo9sUUP8G8YN8NwHwWV2DPUQvbDvtfaQ`
- [ ] 해커톤 공식 등록 (@nothink_ivan)
- [ ] Builder Code 신청 (ops@pacifica.fi)
- [ ] Faucet 테스트 토큰 수령
- [ ] Private Key .env 설정 (개발팀 — 별도 보안 채널로 전달)
