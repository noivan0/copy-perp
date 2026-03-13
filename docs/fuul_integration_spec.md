# Fuul 레퍼럴 연동 완전 스펙

**작성일:** 2026-03-14  
**버전:** v2 (scrapling 실문서 기반)  
**참조:** https://docs.fuul.xyz  
**SDK 버전:** @fuul/sdk 7.18.0  
**현재 구현:** `fuul/referral.py` (HTTP 직접 호출, SDK 없음)

---

## 1. 개요

Fuul = Web3 레퍼럴/인센티브 플랫폼. 레퍼럴 추적, 포인트 지급, 리더보드를 제공한다.

### Copy Perp 적용 이벤트 흐름

```
유저 클릭 레퍼럴 링크
    ↓
sendPageview()  ← URL에서 ?referrer=CODE 자동 캡처
    ↓
지갑 연결 → identifyUser() (connect_wallet 이벤트)
    ↓
트레이더 팔로우 → follow 이벤트
    ↓
복사 주문 체결 → copy_trade 이벤트 (거래량 기록)
    ↓
레퍼러에게 포인트 지급
```

---

## 2. API 키 발급 절차

### 2-1. 계정 생성 및 키 발급
1. https://app.fuul.xyz 접속 → 계정 생성
2. 인센티브 프로그램 생성 (Copy Perp)
3. **Settings > New API Key** 클릭
4. 키 타입 선택 (아래 표 참조)
5. 이름 입력 → **Create** → 키 복사 (재확인 불가)
6. `.env`에 입력

### 2-2. API 키 4종

| 키 타입 | 위치 | 권한 | Copy Perp 용도 |
|---------|------|------|----------------|
| `read-only` | 프론트엔드 | 읽기 전용 | 리더보드/포인트 표시 |
| `send:tracking_event` | 프론트엔드 | 읽기 + 트래킹 | pageview, connect_wallet |
| **`send:trigger_event`** | **백엔드 전용** | 읽기 + 커스텀 이벤트 | follow, copy_trade ← 핵심 |
| `service_role` | 백엔드 전용 | 전체 권한 | 어디언스 관리 |

> ⚠️ `send:trigger_event` / `service_role`은 **절대 프론트 노출 금지**

### 2-3. 환경변수

```env
FUUL_API_KEY=ft_send_trigger_xxxxxxxxxxxxx    # send:trigger_event (백엔드용 핵심 키)
FUUL_TRACKING_KEY=ft_tracking_xxxxxxxxxx      # send:tracking_event (프론트용)
FUUL_PROJECT_ID=proj_xxxxxxxx                 # 대시보드에서 확인
FUUL_API_URL=https://api.fuul.xyz/api/v1      # 기본값
APP_BASE_URL=https://copy-perp.vercel.app     # 레퍼럴 링크 베이스
```

---

## 3. 이벤트 스키마 (공식 문서 기반)

### 3-1. 기본 이벤트 구조

```json
{
  "name": "이벤트_이름",
  "user": {
    "identifier": "유저_지갑_주소",
    "identifier_type": "solana_address"
  },
  "args": {
    "value": {
      "amount": "금액_최소단위",
      "currency": {
        "name": "POINT"
      }
    }
  },
  "metadata": {
    "tracking_id": "uuid-v4",
    "project_id": "proj_xxxxxxxx"
  }
}
```

**`identifier_type` 가능값:** `evm_address` | `solana_address` | `xrpl_address`

**`amount` 단위:**
- ERC-20: WEI (6자리 소수 = $1 USDC → `"1000000"`)
- POINT: 1 포인트 = `"1000000"` (내부 단위)
- USD: 1달러 = `"1000000"` (6자리)

### 3-2. connect_wallet 이벤트 (HTTP 직접 방식)

```python
# POST https://api.fuul.xyz/api/v1/events
# Authorization: Bearer {send:trigger_event 키}

payload = {
    "metadata": {
        "tracking_id": "uuid-v4"
    },
    "name": "connect_wallet",
    "user": {
        "identifier": "3AHZqrocSguMuo9sUUP8G8YN8NwHwWV2DPUQvbDvtfaQ",
        "identifier_type": "solana_address"
    },
    "signature": "서명값",              # 선택사항 (Solana: signMessage 서명)
    "signature_message": "Sign to verify your identity"
}
```

> **서명 없이도 동작**: signature 필드 생략 가능. 단, 레퍼럴 귀속 정확도 낮아짐.

### 3-3. follow 이벤트

```json
{
  "name": "follow",
  "user": {
    "identifier": "팔로워_주소",
    "identifier_type": "solana_address"
  },
  "args": {
    "value": {
      "amount": "1000000",
      "currency": { "name": "POINT" }
    },
    "trader": "트레이더_주소",
    "timestamp": 1741924800
  },
  "metadata": {
    "tracking_id": "uuid-v4",
    "project_id": "proj_xxxxxxxx",
    "referrer": "레퍼러_주소_또는_코드"
  }
}
```

### 3-4. copy_trade 이벤트

```json
{
  "name": "copy_trade",
  "user": {
    "identifier": "팔로워_주소",
    "identifier_type": "solana_address"
  },
  "args": {
    "value": {
      "amount": "1500000000",
      "currency": { "name": "USD" }
    },
    "trader": "트레이더_주소",
    "symbol": "BTC",
    "side": "bid",
    "order_id": "uuid"
  },
  "metadata": {
    "tracking_id": "uuid-v4",
    "project_id": "proj_xxxxxxxx"
  }
}
```

> `amount = USD 금액 × 1,000,000` → $1,500 거래 = `"1500000000"`

### 3-5. 배치 이벤트 (권장 — API 호출 횟수 절감)

```
POST https://api.fuul.xyz/api/v1/events/batch
Authorization: Bearer {send:trigger_event 키}

[이벤트1, 이벤트2, ...]
```

---

## 4. 레퍼럴 추적 흐름 (공식 문서 상세)

### 4-1. 귀속 작동 원리

```
유저 클릭 ?referrer=CODE → sendPageview() 자동 캡처
    ↓
localStorage에 tracking_id 저장
    ↓
지갑 연결 → identifyUser()/connect_wallet 이벤트
    ↓
이후 전환(follow/trade) → 자동으로 레퍼러 귀속
```

### 4-2. pageview 이벤트 (SDK)

```javascript
import { Fuul } from '@fuul/sdk';
Fuul.init({ apiKey: 'send:tracking_event 키' });

// 모든 페이지 로드 시 호출
await Fuul.sendPageview();
// URL의 ?referrer=CODE 자동 캡처
```

### 4-3. identifyUser (SDK — connect_wallet과 동일)

```javascript
// Solana 지갑 연결 후 호출
await Fuul.identifyUser({
  identifier: '3AHZqroc...',
  identifierType: 'solana_address',
  signature: '서명값',                       // signMessage 결과
  message: 'Sign to verify your identity',   // 서명한 메시지
});
```

### 4-4. identifyUser HTTP 직접 호출 (현재 fuul/referral.py 방식)

```python
import requests

url = "https://api.fuul.xyz/api/v1/events"
payload = {
    "metadata": { "tracking_id": "uuid-v4" },
    "name": "connect_wallet",
    "user": {
        "identifier": "3AHZqroc...",
        "identifier_type": "solana_address"
    },
    "signature": "sig_optional",
    "signature_message": "Sign to verify your identity"
}
headers = {
    "content-type": "application/json",
    "authorization": "Bearer ft_send_trigger_xxxx"
}
response = requests.post(url, json=payload, headers=headers)
```

---

## 5. 레퍼럴 코드 생성/관리

### 5-1. 레퍼럴 링크 (URL 방식)

```
https://copy-perp.vercel.app/?referrer={CODE}
```

Copy Perp 구현: 지갑 주소 앞 8자리를 코드로 사용
```
https://copy-perp.vercel.app/?ref=3AHZqroc
```

### 5-2. SDK 레퍼럴 코드 목록 조회

```javascript
const codes = await Fuul.listReferralCodes({
  user_identifier: '3AHZqroc...',
  identifier_type: 'solana_address',
});
```

### 5-3. 레퍼럴 코드 수락 (서명 필요)

서명 메시지 포맷: **`I am using invite code {code}`** (고정)

```javascript
await Fuul.useReferralCode({
  code: 'abc1234',
  user_identifier: '3AHZqroc...',
  user_identifier_type: 'solana_address',
  signature: '서명값',
  signature_message: 'I am using invite code abc1234',
});
```

---

## 6. 포인트 리더보드 조회

```javascript
// 전체 리더보드 (상위 100명)
const lb = await Fuul.getPointsLeaderboard({});

// 추가 필드 포함
const lb2 = await Fuul.getPointsLeaderboard({
  fields: 'tier,referred_volume,enduser_volume,enduser_revenue',
});
// referred_volume은 USD 기준

// 특정 유저 포인트 조회
const userPts = await Fuul.getPointsLeaderboard({
  user_identifier: '3AHZqroc...',
  identifier_type: 'solana_address',
});

// 유저별 전환 포인트
const byConversion = await Fuul.getUserPointsByConversion({
  user_identifier: '3AHZqroc...',
  identifier_type: 'solana_address',
});
```

> ⏱️ 포인트 데이터는 최대 **1시간** 지연 업데이트

---

## 7. 현재 구현 (`fuul/referral.py`) 상태 비교

| 기능 | 스펙 | 현재 구현 | 갭 |
|------|------|---------|-----|
| connect_wallet 이벤트 | POST /events, name=connect_wallet | ✅ `track_connect_wallet()` | 없음 |
| follow 이벤트 | POST /events, name=follow | ✅ `track_follow()` | 없음 |
| copy_trade 이벤트 | POST /events, name=copy_trade | ✅ `track_copy_trade()` | 없음 |
| 배치 전송 | POST /events/batch | ❌ 단건만 구현 | 개선 권장 |
| pageview 이벤트 | SDK `sendPageview()` | ❌ 미구현 | 프론트 SDK 추가 필요 |
| 포인트 리더보드 | GET /payouts/leaderboard/points | ✅ `get_leaderboard()` | 없음 |
| 레퍼럴 코드 SDK | `listReferralCodes()` | ⚠️ 자체 URL 생성만 | SDK 연동 권장 |
| Mock 모드 | - | ✅ FUUL_API_KEY 없으면 자동 | 없음 |

### 배치 이벤트 전송 개선 (추가 권장)

```python
# fuul/referral.py에 추가할 내용
def send_batch_events(self, events: list) -> dict:
    """배치 이벤트 전송 (API 호출 횟수 절감)"""
    if self.mock:
        return {"ok": True, "mock": True, "count": len(events)}
    return self._post("events/batch", events)
```

---

## 8. SDK 초기화 (프론트엔드 통합 시)

```javascript
// npm install @fuul/sdk
import { Fuul } from '@fuul/sdk';

// 초기화 (앱 루트에서 1회)
Fuul.init({ apiKey: process.env.NEXT_PUBLIC_FUUL_TRACKING_KEY });

// 페이지 로드마다
await Fuul.sendPageview();

// 지갑 연결 후
await Fuul.identifyUser({
  identifier: walletAddress,
  identifierType: 'solana_address',
  signature: signedMsg,
  message: 'Sign to verify your identity',
});
```

---

## 9. 활성화 체크리스트

- [ ] app.fuul.xyz 계정 생성
- [ ] Copy Perp 인센티브 프로그램 생성
- [ ] `send:trigger_event` 키 발급 → `.env FUUL_API_KEY` 입력
- [ ] `send:tracking_event` 키 발급 → 프론트 환경변수
- [ ] `FUUL_PROJECT_ID` 확인 → `.env` 입력
- [ ] 서버 재시작 → `/health/detailed`에서 Mock 모드 해제 확인
- [ ] 테스트 follow 이벤트 1건 → app.fuul.xyz 대시보드 수신 확인
- [ ] 프론트에 `@fuul/sdk` 추가 + `sendPageview()` 호출 추가

---

## 10. 참고 링크

| 문서 | URL |
|------|-----|
| 공식 문서 | https://docs.fuul.xyz |
| API 레퍼런스 | https://fuul.readme.io/reference |
| 커스텀 이벤트 | https://docs.fuul.xyz/developer-guide/sending-custom-events-through-the-api |
| 레퍼럴 추적 | https://docs.fuul.xyz/developer-guide/tracking-referrals-in-your-app |
| SDK npm | https://www.npmjs.com/package/@fuul/sdk |
| 대시보드 | https://app.fuul.xyz |
