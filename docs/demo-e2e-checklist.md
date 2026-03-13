# Demo E2E Checklist — Copy Perp Hackathon

**기준:** 2026-03-13 | **작성:** QA팀장  
**환경:** `http://localhost:8080` (Frontend) · `http://localhost:8001` (Backend)

---

## 사전 체크 (Pre-flight)

| # | 항목 | 확인 방법 | 통과 기준 |
|---|---|---|---|
| P1 | 백엔드 기동 | `GET /health` | `status: ok`, `data_connected: true` |
| P2 | 실시간 데이터 | `/health` → `btc_mark` | 현재 BTC 가격 표시 |
| P3 | 트레이더 DB | `GET /traders` | 최소 5명 이상 |
| P4 | 프론트엔드 | `http://localhost:8080` 접속 | 페이지 로드 < 3s |

---

## Scenario 1: Privy 지갑 연결

### 1-1. 페이지 접속
| 단계 | 액션 | 예상 결과 | 실패 대응 |
|---|---|---|---|
| 1 | `http://localhost:8080` 접속 | 홈 페이지 렌더링, `#wallet-btn` 버튼 표시 | 프론트엔드 재기동 필요 |
| 2 | `#wallet-btn` 상태 확인 | "Connect Wallet" 텍스트 (비인증 상태) | Privy 초기화 실패 → `.env.local` 확인 |

### 1-2. Privy 로그인
| 단계 | 액션 | 예상 결과 | 실패 대응 |
|---|---|---|---|
| 3 | `#wallet-btn` 클릭 | Privy 모달 팝업 | Privy App ID 오류 → NEXT_PUBLIC_PRIVY_APP_ID 확인 |
| 4 | Solana 지갑 연결 (또는 이메일 로그인) | 인증 완료 | Solana 체인 미지원 → Privy 설정 확인 |
| 5 | 연결 후 상태 | 지갑 주소 표시 (`xxxx...xxxx`) | `usePrivy().user.linkedAccounts` 비어있음 → 지갑 재연결 |

**Privy 지갑 주소 추출 코드:**
```typescript
const solanaWallet = user?.linkedAccounts?.find(
  (a) => a.type === 'wallet' && a.chainType === 'solana'
);
const address = solanaWallet?.address; // "3AHZqroc..."
```

**검증 포인트:**
- `address` !== `undefined` → ✅
- `address` 형식: base58, 32~44자 → ✅
- `#wallet-btn` 사라지고 주소 표시 + Disconnect 버튼 → ✅

---

## Scenario 2: 리더보드 확인

### 2-1. 트레이더 목록 렌더링
| 단계 | 액션 | 예상 결과 | 실패 대응 |
|---|---|---|---|
| 6 | 리더보드 섹션 확인 | 트레이더 카드 최소 5개 표시 | `GET /traders` 실패 → 백엔드 상태 확인 |
| 7 | 데이터 정확성 | PnL, Win Rate, 추천 배지 표시 | API 응답 필드 누락 → `/traders` 응답 구조 확인 |
| 8 | 추천 트레이더 배지 | `4UBH19qU` (Win 100%), `A6VY4ZBU` (Win 99%) → 배지 표시 | `TOP5_RECOMMENDED` Set 주소 불일치 → `Leaderboard.tsx` 수정 |

**API 응답 필드 검증:**
```json
GET /traders?limit=10
→ [
    {
      "address": "EcX5xSDT45...",
      "alias": "EcX5xSDT",
      "total_pnl": 516000,
      "win_rate": 0.72,       // 0~1 범위 (72%)
      "pnl_7d": 513000,
      "pnl_30d": ...,
      "equity": 628000,
      "composite_score": ...
    }, ...
  ]
```

**win_rate 범위 확인:** `0 ≤ win_rate ≤ 1` (프론트에서 `%` 변환)

### 2-2. 실시간 BTC 가격
| 단계 | 액션 | 예상 결과 | 실패 대응 |
|---|---|---|---|
| 9 | 가격 표시 영역 확인 | 현재 BTC 가격 ($72,000 내외) | `GET /health` → `btc_mark` 비어있음 → DataCollector 재시작 |

---

## Scenario 3: 트레이더 팔로우

### 3-1. 팔로우 플로우
| 단계 | 액션 | 예상 결과 | 실패 대응 |
|---|---|---|---|
| 10 | 트레이더 카드 → "Follow" 버튼 클릭 | 팔로우 모달 팝업 | 미인증 상태 → 로그인 먼저 |
| 11 | Copy Ratio 설정 (기본: 1.0) | 슬라이더/입력 정상 | UI 렌더링 오류 → 브라우저 콘솔 확인 |
| 12 | Max Position USDC 설정 (기본: 100) | 값 입력 정상 | - |
| 13 | "Confirm Follow" 클릭 | `POST /follow` 요청 전송 | 네트워크 오류 → API URL 확인 |

**API 요청 형식:**
```json
POST /follow
{
  "trader_address": "EcX5xSDT45Nvhi2g...",
  "follower_address": "<Privy 지갑 주소>",
  "copy_ratio": 1.0,
  "max_position_usdc": 100
}
```

### 3-2. 팔로우 결과 검증
| 단계 | 액션 | 예상 결과 | 실패 대응 |
|---|---|---|---|
| 14 | 성공 응답 | `{"success": true, "trader": "...", "follower": "..."}` | 중복 팔로우 → 409 응답 (정상) |
| 15 | DB 저장 확인 | `GET /followers/list` → 팔로워 목록에 포함 | DB 오류 → 백엔드 로그 확인 |
| 16 | PositionMonitor 시작 | `GET /health` → `active_monitors` 증가 | 모니터 시작 실패 → 백엔드 재시작 |

---

## Scenario 4: 복사 주문 체결

### 4-1. 트레이더 포지션 변화 감지
| 단계 | 액션 | 예상 결과 | 실패 대응 |
|---|---|---|---|
| 17 | 팔로우 트레이더가 주문 체결 | RestPositionMonitor가 포지션 변화 감지 | 모니터 미기동 → `/health` active_monitors 확인 |
| 18 | Copy Engine 작동 | 팔로워 계정으로 동일 방향 주문 전송 | `builder_code=None` 상태면 400 응답 → **정상** (주문 자체 실행 시도) |
| 19 | DB 기록 | `GET /trades` → 복사 거래 기록 | `failed` 상태도 기록됨 (정상) |

**builder_code 없는 주문 동작:**
```
builder_code=None → 주문 실행 시도 → API 서버 도달 → 400 (미승인) or 200 (성공)
→ Copy Engine: status="failed" 기록 → 서비스 계속 유지 ✅
```

### 4-2. 주문 결과 확인
| 단계 | 액션 | 예상 결과 | 실패 대응 |
|---|---|---|---|
| 20 | `GET /trades` 조회 | 복사 거래 목록 반환 | 빈 목록 → 트레이더 포지션 변화 없음 |
| 21 | `status` 필드 확인 | `filled` (성공) or `failed` (builder 미승인) | `error` → 네트워크 오류 |

---

## Scenario 5: 포지션 확인

| 단계 | 액션 | 예상 결과 | 실패 대응 |
|---|---|---|---|
| 22 | `GET /traders/{address}` 조회 | 트레이더 상세 + 현재 포지션 | 404 → 주소 오타 |
| 23 | 팔로워 포지션 확인 | Pacifica UI에서 팔로워 계정 포지션 확인 | builder 미승인으로 주문 실패 시 포지션 없음 (정상) |
| 24 | PnL 계산 | 복사 거래 기록 기반 수익 계산 | `total_pnl_usdc: 0` → filled 거래 없음 |

---

## 전체 플로우 요약

```
Privy 로그인
    ↓ 지갑 주소 추출 (Solana)
리더보드 확인
    ↓ 추천 트레이더 선택
팔로우 설정 (copy_ratio, max_position_usdc)
    ↓ POST /follow
DB 등록 + PositionMonitor 시작
    ↓ 트레이더 포지션 변화 감지
Copy Engine → 팔로워 주문 전송 (builder_code=noivan)
    ↓
주문 체결 (Pacifica API) → DB 기록
    ↓
/trades 에서 복사 거래 내역 확인
```

---

## API 엔드포인트 빠른 참조

| 엔드포인트 | 메서드 | 설명 |
|---|---|---|
| `/health` | GET | 백엔드 상태 + 실시간 가격 |
| `/traders` | GET | 트레이더 리더보드 (`?limit=20`) |
| `/traders/{address}` | GET | 트레이더 상세 |
| `/follow` | POST | 트레이더 팔로우 |
| `/followers/list` | GET | 팔로워 목록 |
| `/trades` | GET | 복사 거래 내역 |
| `/stats` | GET | 플랫폼 통계 |
| `/signals` | GET | 펀딩비 신호 |

---

## 알려진 이슈 및 제한

| 이슈 | 상태 | 대응 |
|---|---|---|
| builder_code `noivan` 미승인 | ⏳ Pacifica 팀 승인 대기 | `status=failed` 기록, 서비스 유지 |
| WS HMG 차단 | ✅ 해결 | CloudFront SNI + REST 폴링으로 대체 |
| win_rate 자동 갱신 | ✅ 6시간 스케줄러 | 즉시 갱신: `scripts/collect_trader_stats.py` |
| 팔로워 실계정 | ⚠️ 테스트 더미 | Privy 연동 후 실계정으로 교체 필요 |
