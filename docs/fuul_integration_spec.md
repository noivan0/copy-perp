# Fuul 레퍼럴 연동 완전 스펙

**작성일:** 2026-03-14  
**대상:** Copy Perp 개발팀  
**참조:** https://docs.fuul.xyz  
**현재 구현:** `fuul/referral.py` (HTTP 직접 호출, SDK 없음)

---

## 1. 개요

Fuul은 Web3 레퍼럴/인센티브 프로그램 플랫폼입니다.

### Copy Perp에서의 역할
| 이벤트 | 시점 | 목적 |
|--------|------|------|
| `connect_wallet` | 지갑 연결 시 | 유저 추적 세션 시작 |
| `follow` | 트레이더 팔로우 시 | 레퍼럴 전환 기록 |
| `copy_trade` | 복사 주문 체결 시 | 거래 볼륨 기록 |
| `trade_volume` | 거래량 누적 시 | 포인트 보상 계산 |

---

## 2. API 키 발급

### 2-1. 발급 절차
1. https://app.fuul.xyz 접속 (계정 생성 필요)
2. **Settings > New API Key** 클릭
3. Key 타입 선택 (아래 표 참조)
4. 이름 입력 후 **Create**
5. 키 복사 → `.env`에 입력 (재확인 불가)

### 2-2. API 키 타입

| 키 타입 | 사용 위치 | 권한 | Copy Perp 용도 |
|---------|----------|------|----------------|
| `read-only` | 프론트엔드 | 읽기만 | 리더보드 표시 |
| `send:tracking_event` | 프론트엔드 | 읽기 + 트래킹 | 지갑 연결 추적 |
| `send:trigger_event` | **백엔드 전용** | 읽기 + 커스텀 이벤트 | follow/copy_trade 이벤트 |
| `service_role` | **백엔드 전용** | 전체 권한 | 어디언스 관리 |

> ⚠️ `send:trigger_event`와 `service_role`은 절대 프론트엔드 노출 금지

### 2-3. 환경변수 설정

```env
# .env
FUUL_API_KEY=ft_send_trigger_xxxxxxxxxxxxx  # send:trigger_event 키
FUUL_PROJECT_ID=proj_xxxxxxxx              # 프로젝트 ID (대시보드에서 확인)
FUUL_API_URL=https://api.fuul.xyz/api/v1   # 기본값
APP_BASE_URL=https://copy-perp.vercel.app  # 레퍼럴 링크 기본 URL
```

---

## 3. 이벤트 스키마

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
      "amount": "1000000",
      "currency": { "name": "POINT" }
    }
  },
  "metadata": {
    "tracking_id": "uuid-v4",
    "project_id": "proj_xxxxxxxx",
    "referrer": "레퍼러_주소_또는_코드"
  }
}
```

### 3-2. Copy Perp 이벤트 정의

#### `connect_wallet` — 지갑 연결 이벤트
```json
{
  "name": "connect_wallet",
  "user": {
    "identifier": "3AHZqroc...주소",
    "identifier_type": "solana_address"
  },
  "args": {
    "page": "/",
    "locationOrigin": "https://copy-perp.vercel.app"
  },
  "metadata": {
    "tracking_id": "uuid-v4"
  }
}
```

#### `follow` — 트레이더 팔로우 이벤트
```json
{
  "name": "follow",
  "user": {
    "identifier": "팔로워_주소",
    "identifier_type": "solana_address"
  },
  "args": {
    "trader": "트레이더_주소",
    "timestamp": 1741924800
  },
  "metadata": {
    "tracking_id": "uuid-v4",
    "referrer": "레퍼러_주소"
  }
}
```

#### `copy_trade` — 복사 주문 체결 이벤트
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
    "tracking_id": "uuid-v4"
  }
}
```
> ⚠️ `amount`는 최소 단위(USD의 경우 소수점 6자리 = $1.50 → `1500000`)

#### `trade_volume` — 거래 볼륨 누적 이벤트
```json
{
  "name": "trade_volume",
  "user": {
    "identifier": "팔로워_주소",
    "identifier_type": "solana_address"
  },
  "args": {
    "value": {
      "amount": "50000000",
      "currency": { "name": "USD" }
    },
    "period_7d": true,
    "symbol": "BTC"
  },
  "metadata": {
    "tracking_id": "uuid-v4"
  }
}
```

---

## 4. API 엔드포인트

### 4-1. 단건 이벤트 전송

```
POST https://api.fuul.xyz/api/v1/events
Authorization: Bearer {send:trigger_event 키}
Content-Type: application/json

{body: 위 이벤트 스키마}
```

### 4-2. 배치 이벤트 전송 (권장)

```
POST https://api.fuul.xyz/api/v1/events/batch
Authorization: Bearer {send:trigger_event 키}
Content-Type: application/json

[이벤트1, 이벤트2, ...]
```

### 4-3. 포인트 리더보드 조회

```
GET https://api.fuul.xyz/api/v1/payouts/leaderboard/points
Authorization: Bearer {read-only 키}

응답: 상위 100명의 포인트 순위
```

### 4-4. 개별 유저 포인트 조회

```
GET https://api.fuul.xyz/api/v1/payouts/leaderboard/points?user_identifier={주소}&identifier_type=solana_address
Authorization: Bearer {read-only 키}
```

---

## 5. 레퍼럴 코드 생성/관리

### 5-1. 레퍼럴 코드 생성

SDK (`@fuul/sdk`) 기준:
```ts
const result = await Fuul.listReferralCodes({ user_identifier: '주소' });
// 없으면 자동 생성됨
```

### 5-2. 레퍼럴 코드 수락

유저가 `?referrer=CODE` URL로 방문 → SDK `sendPageview()` → 레퍼럴 자동 귀속

코드 직접 수락 (서명 필요):
```ts
await Fuul.useReferralCode({
  code: 'abc1234',
  user_identifier: '주소',
  user_identifier_type: 'solana_address',
  signature: '서명값',
  signature_message: 'I am using invite code abc1234',  // 고정 포맷
});
```

### 5-3. 레퍼럴 링크 생성

```
https://copy-perp.vercel.app/?referrer={레퍼럴_코드}
```

Copy Perp에서는 지갑 주소 앞 8자리를 코드로 사용:
```
https://copy-perp.vercel.app/?ref=3AHZqroc
```

---

## 6. 현재 구현 상태

`fuul/referral.py` — HTTP 직접 호출 방식 (React SDK 없음):

```python
from fuul.referral import get_fuul

fuul = get_fuul()

# 지갑 연결 추적
await fuul.track_connect_wallet(address)

# 팔로우 이벤트
await fuul.track_follow(follower_address, trader_address, referrer)

# 복사 주문 이벤트
fuul.track_copy_trade(
    follower_address=follower,
    trader_address=trader,
    symbol='BTC',
    side='bid',
    amount_usdc=1500.0,
    order_id='uuid'
)

# 포인트 조회
points = fuul.get_points(address)
leaderboard = fuul.get_leaderboard(limit=10)

# 레퍼럴 링크 생성
link = fuul.generate_referral_link(address)
```

**FUUL_API_KEY 없으면 자동으로 Mock 모드** → 실제 API 호출 없이 로그만 출력.

---

## 7. 활성화 체크리스트

- [ ] `app.fuul.xyz` 계정 생성
- [ ] 인센티브 프로그램 생성 (Copy Perp)
- [ ] `send:trigger_event` 키 발급
- [ ] `read-only` 키 발급 (프론트용)
- [ ] `.env`에 `FUUL_API_KEY`, `FUUL_PROJECT_ID` 입력
- [ ] 서버 재시작 → Mock 모드 해제 확인 (`/health/detailed` 확인)
- [ ] 테스트 이벤트 1건 전송 → 대시보드에서 수신 확인

---

## 8. 참고 링크

- 공식 문서: https://docs.fuul.xyz
- API 레퍼런스: https://fuul.readme.io/reference
- SDK npm: https://www.npmjs.com/package/@fuul/sdk
- 대시보드: https://app.fuul.xyz
