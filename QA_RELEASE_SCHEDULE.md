# P002 Copy-Perp — QA 릴리즈 점검 스케줄
_마감: 2026-04-16 | 작성: QA팀장 Quinn | 기준: 2026-03-18_

---

## 전체 타임라인 (역산)

```
3/18 ──── W1 완료 (인프라/API/Builder Code 확정)
3/22 ──── W1 마감 | QA 체크포인트 #1
3/29 ──── W2 마감 | QA 체크포인트 #2 (카피 로직 E2E)
4/05 ──── W3 마감 | QA 체크포인트 #3 (프론트엔드 E2E)
4/10 ──── 🔴 기능 동결 (Feature Freeze)
4/12 ──── QA 최종 릴리즈 게이트
4/14 ──── 데모 영상 + 문서 완성
4/16 ──── 🏁 해커톤 제출 마감
```

---

## 체크포인트별 QA 기준

### ✅ CP#0 — 현재 (3/18) 완료
- [x] 전체 TC 26 PASS (3 skipped — HMG 환경 제한)
- [x] API 6/6 엔드포인트 정상
- [x] Builder Code 0.001 확정
- [x] Mock 트레이더 DB 정리

---

### 🔲 CP#1 — W1 마감 (3/22)
**기준: 인프라 안정성**

| TC | 항목 | 기준 |
|----|------|------|
| CP1-01 | 테스트 전체 PASS | ≥ 26 PASS |
| CP1-02 | API 서버 24h 무중단 | uptime 100% |
| CP1-03 | 트레이더 데이터 최신화 | 1일 1회 sync |
| CP1-04 | WebSocket 또는 REST폴링 정상 | position 감지 지연 < 1s |

**점검 명령:**
```bash
python3 -m pytest tests/ -q --tb=short -p no:reruns
curl http://localhost:8001/health | python3 -m json.tool
```

---

### 🔲 CP#2 — W2 마감 (3/29)
**기준: 카피 로직 E2E**

| TC | 항목 | 기준 |
|----|------|------|
| CP2-01 | 트레이더 포지션 감지 | 이벤트 수신 → 처리 < 2s |
| CP2-02 | 팔로워 카피 주문 실행 | mock_mode=False 체결 성공 |
| CP2-03 | Builder Code 수수료 포함 | 주문 payload builder_code="noivan" |
| CP2-04 | 팔로워 온보딩 E2E | POST /followers/onboard → 모니터 시작 |
| CP2-05 | 연속 실패 재시도 | max_retries=3 동작 확인 |
| CP2-06 | 테스트 전체 PASS | ≥ 26 PASS |

**릴리즈 블로킹 조건:**
- 카피 주문 실패율 > 20% → 블로킹
- Builder Code 누락 주문 발생 → 즉시 패치

---

### 🔲 CP#3 — W3 마감 (4/5)
**기준: 프론트엔드 + Fuul E2E**

| TC | 항목 | 기준 |
|----|------|------|
| CP3-01 | Privy 지갑 연결 | 소셜 로그인 → 지갑 주소 정상 추출 |
| CP3-02 | 리더보드 렌더링 | 트레이더 ≥ 5명, PnL/WR 표시 |
| CP3-03 | Start Copying 버튼 | onboard API 호출 → 성공 toast |
| CP3-04 | 레퍼럴 링크 생성 | Fuul API 연동 또는 mock |
| CP3-05 | 포트폴리오 시뮬 | /portfolio/backtest 응답 정상 |
| CP3-06 | 모바일 반응형 | 375px 기준 레이아웃 깨짐 없음 |
| CP3-07 | 테스트 전체 PASS | ≥ 26 PASS |

---

### 🔲 CP#4 — 기능 동결 (4/10)
**기준: 안정성 + 데모 준비**

| TC | 항목 | 기준 |
|----|------|------|
| CP4-01 | 전체 E2E 시나리오 수동 통과 | demo-e2e-checklist.md 전항목 |
| CP4-02 | 12h 연속 가동 안정성 | 에러율 < 1%, 메모리 누수 없음 |
| CP4-03 | 동시 팔로워 10명 부하 | 응답시간 < 3s |
| CP4-04 | 에러 핸들링 UX | 500/404 시 사용자 친화 메시지 |
| CP4-05 | 테스트 전체 PASS | ≥ 26 PASS |

---

### 🔲 CP#5 — 최종 릴리즈 게이트 (4/12)
**이 게이트 통과 없이 제출 없음**

| 항목 | 기준 | 상태 |
|------|------|------|
| 전체 테스트 | ≥ 26 PASS | — |
| API 전 엔드포인트 | 200 응답 | — |
| 테스트넷 실거래 | 카피 주문 10건+ 체결 확인 | — |
| 프론트엔드 E2E | 전 시나리오 수동 통과 | — |
| README/문서 | 데모 영상 링크 포함 | — |
| Builder Code | noivan 수수료 실제 수취 확인 | — |

---

## 즉시 알럿 기준 (해커톤 기간 내내)

| 조건 | 조치 |
|------|------|
| 테스트 FAIL 발생 | 즉시 Dev팀 전달 + 당일 수정 |
| API 서버 다운 | 5분 내 재시작 + 원인 분석 |
| 테스트넷 API 장애 | Pacifica Discord 확인 + 대기 |
| 카피 주문 실패율 > 20% | Dev팀 긴급 패치 요청 |

---

## QA 자동화 명령 (일별 실행)

```bash
# 일별 전체 TC 실행
cd /root/.openclaw/workspace/paperclip-company/projects/pacifica-hackathon/copy-perp
python3 -m pytest tests/ -q --tb=short -p no:reruns 2>&1 | tee results/qa_$(date +%Y%m%d).log

# API 헬스체크
python3 -c "
import requests, json
r = requests.get('http://localhost:8001/health', timeout=5)
print(json.dumps(r.json(), indent=2))
"
```

---
_QA 승인 없이 4/16 제출 없음. 각 CP 통과 시 대장에게 즉시 보고._
