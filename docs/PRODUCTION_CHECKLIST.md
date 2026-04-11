# Copy Perp — 프로덕션 품질 체크리스트
> 작성: 전략팀 | 기준: "실제 유저가 사용하는 서비스"
> 업데이트: 2026-04-11

---

## 🎯 핵심 원칙
해커톤 제출용이 아님. 실제 유저가 실제 자산을 맡기는 서비스.
버그 하나가 유저 손실로 직결됨. 완벽한 검증이 기본값.

---

## SECTION 1: 유저 온보딩 E2E

### 1-1. 첫 접속 경험
- [ ] Vercel 프론트 로드 시간 < 3초
- [ ] Privy 지갑 연결 UI 정상 노출
- [ ] 지갑 연결 후 builder code approve 안내 명확
- [ ] 처음 접속한 유저가 "무엇을 해야 하는지" 3초 내 이해 가능

### 1-2. 첫 팔로우 플로우
- [ ] 트레이더 리더보드 → 팔로우 버튼 → POST /follow 성공
- [ ] copy_ratio, max_position_usdc 입력 검증 (0이하, 범위 초과 시 사용자 친화적 에러)
- [ ] 팔로우 성공 후 피드백 메시지 노출
- [ ] 중복 팔로우 시 적절한 에러 (DB UNIQUE 제약 → 사용자 친화적 메시지)

### 1-3. 언팔로우 플로우  
- [ ] DELETE /follow/{trader} + follower_address 파라미터 정상 작동
- [ ] 언팔로우 후 모니터 중지 확인

---

## SECTION 2: API 안정성

### 2-1. 상태코드 일관성
| 상황 | 기대 코드 |
|------|-----------|
| 정상 조회 | 200 |
| 유효성 검증 실패 | 400 |
| 주소 형식 오류 | 400 |
| 존재하지 않는 리소스 | 404 |
| Rate limit 초과 | 429 + Retry-After 헤더 |
| 서버 오류 | 500 (detail은 DEBUG 모드에서만) |

### 2-2. 응답 형식 일관성
모든 에러 응답에 반드시 포함:
```json
{
  "error": "사람이 읽을 수 있는 메시지",
  "code": "MACHINE_READABLE_CODE",
  "request_id": "8자리 식별자"
}
```

### 2-3. Rate Limit
- [ ] /health: 분당 180회 (k8s probe 허용)
- [ ] /markets: 분당 120회
- [ ] /follow: 분당 20회
- [ ] /trades: 분당 60회
- [ ] 429 응답에 Retry-After 헤더 포함

---

## SECTION 3: 데이터 영속성

### 3-1. Render DB 설정
- [ ] DB_PATH = /var/data/copy_perp.db (Render Disk 마운트)
- [ ] 재배포 후 팔로워/트레이더 데이터 유지
- [ ] WAL 모드 활성화 확인
- [ ] 마이그레이션 자동 적용 확인

### 3-2. 포지션 영속성
- [ ] 서버 재시작 후 follower_positions DB → 메모리 복원
- [ ] copy_trades 기록 영속
- [ ] fee_records 누적 영속

---

## SECTION 4: 보안

### 4-1. 입력 검증
- [ ] Solana 주소: base58 + 32바이트 검증
- [ ] SQL Injection: 파라미터 바인딩 (? 사용)
- [ ] 대용량 페이로드: FastAPI 기본 제한 적용 확인
- [ ] XSS: JSON 응답이므로 기본 안전, 단 에러 메시지에 사용자 입력 반사 없음

### 4-2. 보안 헤더
- [ ] X-Content-Type-Options: nosniff
- [ ] X-Frame-Options: DENY
- [ ] Strict-Transport-Security (HTTPS)
- [ ] X-Request-ID (추적성)

### 4-3. CORS
- [ ] Vercel 프론트 도메인만 허용
- [ ] evil.com 등 임의 origin 차단
- [ ] credentials: true 설정 확인

### 4-4. 민감 정보
- [ ] AGENT_PRIVATE_KEY 로그 미출력
- [ ] /config 엔드포인트에 키 미노출
- [ ] DEBUG=false 프로덕션에서 스택트레이스 미출력

---

## SECTION 5: 성능

### 5-1. 응답 시간 기준
| 엔드포인트 | 목표 |
|-----------|------|
| /healthz | < 50ms |
| /health | < 200ms |
| /markets | < 500ms |
| /trades | < 1000ms |
| /stats | < 1000ms |

### 5-2. 백그라운드 루프
- [ ] _sync_leaderboard_loop: 60초 주기 정상 실행
- [ ] _winrate_refresh_loop: 6시간 주기 정상 실행
- [ ] RestPositionMonitor: 3초 주기 폴링 정상
- [ ] StopLossMonitor: 30초 주기 스캔 정상

---

## SECTION 6: 에러 UX (유저 경험)

### 6-1. 사용자 친화적 에러 메시지
- [ ] "Invalid Solana address" → 유저가 이해 가능
- [ ] "copy_ratio must be between 0.01 and 1.0" → 명확한 범위 안내
- [ ] 500 에러: "Something went wrong. Please try again." (기술 세부 숨김)
- [ ] 429: "Too many requests. Please wait 60 seconds."

### 6-2. 프론트 에러 처리
- [ ] API 오류 시 스피너 무한 로딩 없음
- [ ] 네트워크 오류 시 재시도 안내
- [ ] 주소 연결 안 됨 시 적절한 안내

---

## SECTION 7: 모니터링

### 7-1. /metrics Prometheus
- [ ] copy_perp_active_traders
- [ ] copy_perp_active_followers  
- [ ] copy_perp_copy_trades_total
- [ ] copy_perp_monitors_active
- [ ] copy_perp_btc_price

### 7-2. /events
- [ ] 최근 시스템 이벤트 조회 가능
- [ ] 에러 요약 포함

### 7-3. /health/detailed
- [ ] DB 상태 (ok/fail)
- [ ] 각 모니터 last_poll_ago_sec
- [ ] data_collector 연결 상태

---

## 자율 개선 사이클
```
테스트 에이전트 (매 싸이클)
  → 이슈 발견
  → 코드 수정 (Dev)
  → py_compile 검증
  → git commit + push
  → Render 자동 배포 (2-3분)
  → 재테스트
  → 2회 클린 패스 확인
  → 다음 섹션 진행
```

---

*이 문서는 각 테스트 사이클 후 결과 반영하여 지속 업데이트*
