# Builder Code `noivan` — 완전 적용 가이드
**작성:** 전략팀장 | 2026-03-14  
**상태:** 서버 승인 완료 ✅ / 계정 approve 미완료 ⏳  
**소스:** https://pacifica.gitbook.io/docs/builder-program (직접 확인)

---

## 현재 상태 진단

```
builder/overview?account=3AHZqroc... → []          # Builder Code 등록 확인 필요
account/builder_codes/approvals?account=3AHZqroc... → data: []  # 아직 approve 안됨
```

**Builder Code 승인 ≠ 계정 approve**
- Pacifica 서버에 `noivan` 코드 등록됨 (승인 완료)
- 하지만 우리 계정(`3AHZqroc...`)이 `noivan`을 **사용하겠다고 서명(approve)** 해야 주문에 포함 가능
- **팔로워 각각이 approve 서명**을 해야 해당 계정 주문에 builder_code 적용됨

---

## Step 1: 계정 Approve (현재 미완료)

### 서명 구조 (문서 정확 확인)

```python
# 서명 대상 payload
data_to_sign = {
    "timestamp": int(time.time() * 1000),
    "expiry_window": 5000,
    "type": "approve_builder_code",
    "data": {
        "builder_code": "noivan",
        "max_fee_rate": "0.001"   # 0.1% — builder fee_rate 이상이어야 함
    }
}
```

### 서명 절차 (5단계)

```python
import json, time, base58
from solders.keypair import Keypair

def sort_json_keys(value):
    if isinstance(value, dict):
        return {k: sort_json_keys(value[k]) for k in sorted(value.keys())}
    elif isinstance(value, list):
        return [sort_json_keys(i) for i in value]
    return value

# 1. payload 구성
ts = int(time.time() * 1000)
data_to_sign = {
    "timestamp": ts,
    "expiry_window": 5000,
    "type": "approve_builder_code",
    "data": {"builder_code": "noivan", "max_fee_rate": "0.001"}
}

# 2. 재귀 키 정렬
sorted_payload = sort_json_keys(data_to_sign)

# 3. compact JSON (공백 없음)
compact_json = json.dumps(sorted_payload, separators=(',', ':'))

# 4. UTF-8 → Ed25519 서명
keypair = Keypair.from_bytes(base58.b58decode(MAIN_PRIVATE_KEY))
sig = keypair.sign_message(compact_json.encode('utf-8'))
sig_b58 = base58.b58encode(bytes(sig)).decode('ascii')

# 5. 최종 request body (data 래퍼 제거, top-level flatten)
request_body = {
    "account": "3AHZqrocSguMuo9sUUP8G8YN8NwHwWV2DPUQvbDvtfaQ",
    "agent_wallet": None,
    "signature": sig_b58,
    "timestamp": ts,
    "expiry_window": 5000,
    "builder_code": "noivan",
    "max_fee_rate": "0.001"
}

# POST https://api.pacifica.fi/api/v1/account/builder_codes/approve
```

### ⚠️ 핵심 주의사항

| 항목 | 내용 |
|------|------|
| **서명 키** | **Main account private key** 필수 (Agent key로 불가) |
| **account 필드** | 주문자(팔로워) 지갑 주소 |
| **max_fee_rate** | builder fee_rate 이상 설정 (너무 낮으면 403) |
| **data 래퍼** | 서명 시엔 `"data"` 안에, 최종 body에선 제거(flatten) |
| **Mainnet 엔드포인트** | `api.pacifica.fi` (test-api 아님) |

---

## Step 2: 주문에 builder_code 포함

승인 완료 후, **모든 copy 주문**에 `builder_code` 필드 추가:

### Market Order (현재 코드 확인)

```python
# pacifica/client.py의 market_order() — 이미 구현됨 ✅
payload = {
    "symbol": symbol,
    "side": side,                    # "bid" or "ask"
    "amount": amount,
    "reduce_only": False,
    "slippage_percent": "0.5",
    "client_order_id": str(uuid.uuid4()),
    "builder_code": "noivan"         # ← 이 필드가 핵심
}
# type: "create_market_order"
# endpoint: POST /api/v1/orders/create_market
```

### 서명 시 builder_code 위치

```python
# ⚠️ builder_code는 반드시 "data" 안에 포함해서 서명
data_to_sign = {
    "timestamp": ts,
    "expiry_window": 30000,
    "type": "create_market_order",
    "data": {
        "symbol": "BTC",
        "side": "bid",
        "amount": "0.1",
        "slippage_percent": "0.5",
        "reduce_only": False,
        "client_order_id": "uuid...",
        "builder_code": "noivan"     # ← data 안에 포함해서 서명
    }
}

# 최종 request body (data 래퍼 제거)
final_body = {
    "account": follower_address,
    "agent_wallet": agent_pubkey,    # Agent key 사용 시
    "signature": sig_b58,
    "timestamp": ts,
    "expiry_window": 30000,
    "symbol": "BTC",
    "side": "bid",
    "amount": "0.1",
    "slippage_percent": "0.5",
    "reduce_only": False,
    "client_order_id": "uuid...",
    "builder_code": "noivan"         # ← top-level에도 포함
}
```

---

## Step 3: 지원 엔드포인트 전체

| 엔드포인트 | type | builder_code 위치 |
|---|---|---|
| `POST /orders/create_market` | `create_market_order` | data 안 + top-level |
| `POST /orders/create` | `create_order` | data 안 + top-level |
| `POST /orders/stop/create` | `create_stop_order` | data 안 + top-level |
| `POST /positions/tpsl` | `set_position_tpsl` | data 안 (top-level만, tp/sl 객체 내부엔 X) |

---

## Step 4: 수익 모니터링 엔드포인트

```
# 특정 builder_code로 발생한 거래 조회
GET /api/v1/trades/history?account={wallet}&builder_code=noivan

# Builder Code 전체 스펙/수수료 정보
GET /api/v1/builder/overview?account={our_account}

# Builder Code로 발생한 전체 거래 내역
GET /api/v1/builder/trades?builder_code=noivan

# Builder Code 사용 유저 리더보드
GET /api/v1/leaderboard/builder_code?builder_code=noivan
```

---

## 현재 코드 상태 체크

```
pacifica/client.py          ✅ market_order에 builder_code 파라미터 구현됨
pacifica/client.py          ✅ approve_builder_code() 함수 구현됨
pacifica/builder_code.py    ✅ 서명 로직 구현됨
core/copy_engine.py         ✅ builder_code=bc 주문 전달 구현됨
scripts/approve_builder_code.py ✅ approve 스크립트 존재
.env                        ⚠️ NETWORK=testnet (mainnet 전환 필요)
```

---

## 즉시 실행 액션 (개발팀)

### 1. Approve 실행 (Main Private Key 필요)
```bash
cd /path/to/copy-perp
NETWORK=mainnet \
MAIN_PRIVATE_KEY=<3AHZqroc 계정의 private key> \
python3 scripts/approve_builder_code.py
```

### 2. Approve 확인
```bash
python3 -c "
import urllib.request, json
url = 'https://api.codetabs.com/v1/proxy/?quest=https://api.pacifica.fi/api/v1/account/builder_codes/approvals?account=3AHZqrocSguMuo9sUUP8G8YN8NwHwWV2DPUQvbDvtfaQ'
print(json.loads(urllib.request.urlopen(url).read()))
"
```

### 3. NETWORK=mainnet 전환 후 테스트 주문 실행
```bash
NETWORK=mainnet python3 scripts/demo_run.py
```

---

## 에러 처리

| 에러 | 원인 | 해결 |
|---|---|---|
| `403 Unauthorized` | approve 미완료 or max_fee_rate 낮음 | approve 재실행, max_fee_rate=0.001 확인 |
| `404 Not Found` | builder_code 미등록 | 이미 승인됨 — 코드명 오타 확인 |
| `400 Bad Request` | 코드 형식 오류 | 영숫자, 최대 16자 확인 (`noivan` 정상) |
| 서명 오류 | Agent key로 approve 시도 | Main account key로 재시도 |

---

## 수익 구조 요약

```
팔로워 주문 체결
    → builder_code=noivan 포함
    → Pacifica가 수수료 일부를 noivan Builder Code로 귀속
    → /builder/trades API로 실시간 확인 가능
    → Pacifica 포인트 프로그램 (10M points pool) 기여로 카운팅
```

**Fee Rate 0.001 = 거래액의 0.1%**  
팔로워 $10,000 복사 거래 → $10 수수료 → noivan 계정에 귀속

---

*전략팀장 작성 | 개발팀 즉시 실행 가능한 형태로 정리됨*
