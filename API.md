# Copy Perp — API Reference

> Base URL: `https://copy-perp.onrender.com`  
> 모든 응답은 JSON (Content-Type: application/json)  
> 프로덕션에서 `/docs` 및 `/redoc`는 `DEBUG=true` 시에만 노출

---

## 인증

현재 버전에서는 API Key 인증 없이 공개 접근 가능합니다.  
단, Rate Limit이 적용되며 일부 관리자 엔드포인트는 `X-Admin-Key` 헤더가 필요합니다.

---

## Rate Limit

엔드포인트별 차등 적용 (sliding window, IP 기준):

| 엔드포인트 | 제한 |
|-----------|------|
| `/traders`, `/markets` | 분당 120회 |
| `/trades`, `/stats` | 분당 60회 |
| `/follow`, `/follow/{addr}` | 분당 20회 |
| `/signals`, `/referral/*` | 분당 30회 |
| `/traders/ranked` | 분당 30회 |
| `/health` | 분당 180회 |

초과 시 `429 Too Many Requests` + `Retry-After` 헤더 반환.

---

## 에러 응답 형식

```json
{
  "error": "Human-readable message",
  "code": "MACHINE_READABLE_CODE",
  "request_id": "a1b2c3d4",
  "field": "optional_field_name"
}
```

---

## 엔드포인트 목록

### 상태 / 헬스

#### `GET /`
서비스 상태 또는 프론트엔드 index.html

#### `GET /healthz`
Render 헬스체크용 (빠른 응답)
```json
{
  "status": "ok",
  "db_ok": true,
  "db_active_traders": 133,
  "last_leaderboard_sync_ts": 1712800000,
  "env_degraded": false,
  "active_monitors": 12,
  "startup_at": 1712790000,
  "revision": "abc1234"
}
```
- DB 장애 시 `503` + `"status": "degraded"` → Render 재시작 트리거

#### `GET /health`
실시간 상태 (BTC 가격, 모니터 목록 포함)
```json
{
  "status": "ok",
  "network": "testnet",
  "btc_mark": "83500.5",
  "btc_funding": "0.00012",
  "active_monitors": 12,
  "uptime_seconds": 3600.1,
  "version": "1.3.4"
}
```

#### `GET /health/detailed`
상세 헬스 (DB 집계, 모니터 상태, 데이터 수집기 상태)

#### `GET /metrics`
Prometheus 텍스트 형식 메트릭
- `copy_perp_active_traders`, `copy_perp_active_followers`
- `copy_perp_copy_trades_total`, `copy_perp_volume_usdc`
- `copy_perp_memory_rss_bytes`, `copy_perp_rate_limit_keys` (R9 추가)

#### `GET /events`
시스템 이벤트 로그 (최근 50건 기본)
- `?limit=N` — 최대 반환 수
- `?level=error|warn|info` — 레벨 필터

---

### 마켓 / 시그널

#### `GET /markets`
실시간 마켓 데이터 (펀딩비 내림차순)
```json
{
  "data": [
    {"symbol": "BTC", "mark": "83500.5", "funding": "0.00012", "oracle": "83490.0", "open_interest": "1234567"}
  ],
  "count": 45
}
```
- `?symbol=BTC` — 단일 심볼 조회

#### `GET /signals`
펀딩비 극단 + Oracle-Mark 괴리 시그널
```json
{
  "ok": true,
  "funding_extremes": [...],
  "oracle_mark_divergence": [...],
  "excluded_risk_markets": [...]
}
```
- `?top_n=5` — 상위 N개 (기본 5, 최대 50)

---

### 트레이더

#### `GET /traders`
트레이더 목록 (DB 기반)
- `?mock=true` — 모의 데이터 반환 (테스트용)

#### `POST /traders`
트레이더 등록 + 포지션 모니터링 시작
```json
{ "address": "EcX5xSDT45..." }
```

#### `GET /traders/{address}`
트레이더 상세 정보 (CRS 점수 포함)

#### `GET /traders/{address}/trades`
트레이더 체결 이력

#### `GET /traders/{address}/followers`
팔로워 목록

#### `GET /traders/ranked`
CRS(Copy Reliability Score) 랭킹
```json
{
  "data": [
    {
      "address": "EcX5x...",
      "alias": "whale_01",
      "crs": 72.5,
      "grade": "B",
      "win_rate": 0.64,
      "pnl_30d": 4200.0,
      "copyability": 85,
      "disqualified": false
    }
  ],
  "count": 20
}
```

---

### 팔로우

#### `POST /follow`
팔로우 시작 + 포지션 모니터 등록
```json
{
  "follower_address": "3rXoG6i...",
  "trader_address": "EcX5xSD...",
  "copy_ratio": 0.5,
  "max_position_usdc": 50.0,
  "referrer_address": null
}
```
응답:
```json
{
  "status": "ok",
  "follower": "3rXoG6i...",
  "trader": "EcX5xSD...",
  "builder_code": "noivan",
  "monitoring": true
}
```

#### `DELETE /follow/{trader_address}`
팔로우 중지
- Query: `?follower_address=3rXoG6i...`
- 또는 Body: `{ "follower_address": "3rXoG6i..." }`

---

### 거래 내역

#### `GET /trades`
복사 거래 내역
- `?limit=50` — 반환 수 (최대 500)
- `?follower=<addr>` — 팔로워 필터
- `?trader=<addr>` — 트레이더 필터
- `?status=filled|pending|failed` — 상태 필터

응답 `summary`:
```json
{
  "total": 1234,
  "filled": 1100,
  "failed": 50,
  "realized_pnl_usdc": 420.5,
  "total_volume_usdc": 98000.0,
  "total_fee_usdc": 0.098,
  "win_rate_pct": 64.2
}
```

---

### 통계

#### `GET /stats`
플랫폼 전체 통계 (30초 캐시)
```json
{
  "active_traders": 133,
  "active_followers": 47,
  "total_trades_filled": 890,
  "total_volume_usdc": 98000.0,
  "builder_fee_total_usdc": 0.098,
  "network": "testnet",
  "version": "1.3.4",
  "cached": false
}
```

---

### 레퍼럴

#### `GET /referral/{address}`
레퍼럴 링크 + 포인트
```json
{
  "address": "3rXoG6i...",
  "referral_link": "https://copy-perp.onrender.com?ref=3rXoG6i",
  "points": 150
}
```

#### `POST /fuul/track`
레퍼럴 추적 (팔로우 시 자동 호출)
```json
{ "referrer": "3rXoG6i...", "referee": "5C9GKLr..." }
```

#### `GET /fuul/leaderboard`
레퍼럴 리더보드 (`?limit=10`)

---

### 설정

#### `GET /config`
프론트엔드용 공개 설정 (민감 정보 제외)
```json
{
  "privy_app_id": "cmmvoxcix...",
  "privy_configured": true,
  "builder_code": "noivan",
  "builder_fee_rate": "0.001",
  "network": "testnet",
  "mock_mode": false,
  "fuul_enabled": true
}
```
> ⚠️ 이 엔드포인트는 `PRIVATE_KEY`, `API_KEY` 류를 반환하지 않습니다.

---

### 실시간 스트림

#### `GET /stream`
Server-Sent Events (SSE) — 5초 주기 실시간 업데이트
```
data: {"btc_mark":"83500.5","active_traders":133,"trades_filled":890,"ts":1712800000}
```

---

### 관리자 (Admin)

#### `POST /admin/sync`
Leaderboard 수동 재동기화  
`X-Admin-Key: $ADMIN_API_KEY` 헤더 필수.  
`ADMIN_API_KEY` 미설정 시 비활성화 (`503`).

---

### PnL / 트래커

#### `GET /pnl/{follower_address}`
팔로워 PnL 실적 조회

#### `GET /pnl/{follower_address}/trades`
팔로워 복사 거래 이력 (페이지네이션)
- `?limit=50&offset=0&status=filled`

#### `GET /tracker/{address}`
메인넷 장기 PnL 추적

---

### 퍼포먼스 / 포트폴리오

#### `GET /performance/{follower_address}`
팔로워 실적 기록

#### `GET /portfolio/{address}`
포트폴리오 현황

---

### 페이퍼트레이딩

#### `GET /papertrading/status`
4개 전략 페이퍼트레이딩 현황

---

## 환경 변수 (배포 시 필수)

| 변수 | 설명 | 기본값 |
|------|------|--------|
| `AGENT_PRIVATE_KEY` | Agent Key (주문 서명) | — (필수) |
| `AGENT_WALLET` | Agent 공개키 | — (필수) |
| `ACCOUNT_ADDRESS` | Pacifica 계정 주소 | — (필수) |
| `BUILDER_CODE` | Builder Code | `noivan` |
| `BUILDER_FEE_RATE` | 빌더 수수료율 | `0.001` |
| `NETWORK` | `testnet` or `mainnet` | `testnet` |
| `DB_PATH` | SQLite 파일 경로 | `copy_perp.db` |
| `PRIVY_APP_ID` | Privy App ID | — |
| `ALLOWED_ORIGINS` | CORS 허용 Origin (쉼표 구분) | — |
| `TRUSTED_PROXY_IPS` | 신뢰할 프록시 IP/CIDR | — |
| `ADMIN_API_KEY` | `/admin/sync` 인증 키 | — (미설정 시 비활성화) |
| `DEBUG` | `true`이면 `/docs` 노출 | `false` |

---

*API Version: 1.3.4 | Last Updated: 2026-04-11 (R9)*
