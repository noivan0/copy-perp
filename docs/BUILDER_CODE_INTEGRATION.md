# Builder Code 'noivan' 통합 가이드

> **상태**: Pacifica 팀 승인 완료 ✅  
> **작성일**: 2026-03-14 | **작성자**: QA팀장

---

## 핵심 개념 정리

### 왜 Verification failed가 발생하는가?

Pacifica builder code approve는 **account 소유자의 private key**로 서명해야 한다.

```
Account: 3AHZqrocSguMuo9sUUP8G8YN8NwHwWV2DPUQvbDvtfaQ (팔로워 지갑)
API Key (AGENT_WALLET): 9mxJJAQwKLmM3hUdFebFXgkD8TPnDEJCZWhWN2uLZHWi (주문 서명용)

approve_builder_code → 3AHZqroc...의 key로 서명 (main account)
market_order        → 9mxJJAQw...의 key로 서명 (API Key)
```

### 승인된 Builder Code 적용 방법 (2단계)

---

## Step 1: 팔로워 approve_builder_code

### 서명 구조 (문서 기준)

```json
// 서명 대상 (재귀 알파벳 정렬 → compact JSON → Ed25519 → Base58)
{
  "data": {
    "builder_code": "noivan",
    "max_fee_rate": "0.001"
  },
  "expiry_window": 5000,
  "timestamp": 1748970123456,
  "type": "approve_builder_code"
}
```

### POST body (서명 후)

```json
POST https://api.pacifica.fi/api/v1/account/builder_codes/approve

{
  "account": "<팔로워_지갑_주소>",
  "agent_wallet": null,
  "builder_code": "noivan",
  "expiry_window": 5000,
  "max_fee_rate": "0.001",
  "signature": "<base58_서명>",
  "timestamp": 1748970123456
}
```

### 두 가지 승인 방법

#### 방법 A: 프론트엔드(Privy) 서명 플로우 (권장)
```
1. GET /builder/prepare-approval?account=<addr>
   → { message, timestamp, builder_code, max_fee_rate }

2. Privy.signMessage(message) → signature

3. POST /builder/approve { account, signature, timestamp }
   → 서버에서 Pacifica API로 포워딩
```

#### 방법 B: main private key 직접 서명
```bash
python3 scripts/approve_builder_code.py <MAIN_PRIVATE_KEY>
# 또는
MAIN_PRIVATE_KEY=<key> python3 scripts/approve_builder_code.py
```

---

## Step 2: 주문에 builder_code 포함

승인 완료 후, 모든 주문에 `builder_code` 포함:

### market_order

```json
// 서명 대상 data 안에 builder_code 포함
{
  "data": {
    "amount": "0.001",
    "builder_code": "noivan",
    "client_order_id": "uuid",
    "reduce_only": false,
    "side": "bid",
    "slippage_percent": "0.5",
    "symbol": "BTC"
  },
  "expiry_window": 5000,
  "timestamp": 1748970123456,
  "type": "create_market_order"
}
```

```json
// POST body (flatten)
{
  "account": "<addr>",
  "agent_wallet": "<agent_addr>",
  "amount": "0.001",
  "builder_code": "noivan",
  "client_order_id": "uuid",
  "expiry_window": 5000,
  "reduce_only": false,
  "side": "bid",
  "slippage_percent": "0.5",
  "symbol": "BTC",
  "signature": "<base58>",
  "timestamp": 1748970123456
}
```

### TP/SL 주의사항

```
builder_code는 top-level에만 포함 (take_profit/stop_loss 내부에 넣지 말 것)
```

---

## 현재 구현 상태 점검

| 항목 | 상태 | 비고 |
|------|------|------|
| `pacifica/client.py` market_order | ✅ | builder_code payload 안에 포함 |
| `pacifica/client.py` limit_order | ✅ | 기본값 BUILDER_CODE |
| `pacifica/client.py` set_tpsl | ✅ | top-level 포함 |
| `pacifica/builder_code.py` approve | ✅ | 서명 구조 정확 |
| `api/routers/builder.py` | ✅ | Privy 플로우 구현 |
| `scripts/approve_builder_code.py` | ✅ | main key 직접 서명 |
| **account approve 실제 실행** | ⏳ | main account key 필요 |

---

## 검증 방법

### 1. approve 상태 확인

```bash
python3 -c "
import ssl, socket, json
# GET /account/builder_codes/approvals?account=<addr>
# 응답에 builder_code: 'noivan' 항목이 있으면 승인 완료
"
```

### 2. builder/overview 확인

```
GET https://api.pacifica.fi/api/v1/builder/overview?account=<ACCOUNT>
→ builder code 등록 정보
```

### 3. 실제 주문 후 builder/trades 확인

```
GET https://api.pacifica.fi/api/v1/builder/trades?builder_code=noivan
→ 빌더 코드로 발생한 거래 내역
```

---

## 유용한 엔드포인트

```
# 팔로워별 거래 내역 (builder code 필터)
GET /trades/history?account=<WALLET>&builder_code=noivan

# 빌더 코드 스펙
GET /builder/overview?account=<ACCOUNT>

# 빌더 거래 내역
GET /builder/trades?builder_code=noivan

# 빌더 리더보드
GET /leaderboard/builder_code?builder_code=noivan
```

---

## 에러 코드

| 코드 | 원인 | 해결 |
|------|------|------|
| `400 Verification failed` | 서명 key가 account 소유자 key가 아님 | main account key로 서명 |
| `403 Unauthorized` | 팔로워가 미승인 또는 max_fee_rate 너무 낮음 | approve 먼저 실행 |
| `404 Not Found` | builder_code 미존재 | Pacifica 팀 등록 확인 |
| `429 Rate limit` | 요청 너무 많음 | 15초 이상 대기 |
