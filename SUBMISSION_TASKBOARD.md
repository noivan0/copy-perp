# P002 Copy Perp — 제출 태스크보드
> 작성: Dev팀장 | 2026-04-10 | 마감: **2026-04-16**

---

## 🔴 CRITICAL — 노이반님 직접 필요

| # | 태스크 | 방법 | 상태 |
|---|--------|------|------|
| C1 | **Builder Code approve** (MAIN_PRIVATE_KEY 필요) | `python3 scripts/approve_builder_code.py <MAIN_PRIVATE_KEY>` | ⏳ 대기 |
| C2 | **해커톤 제출 폼 제출** | https://forms.gle/zYm9ZBH1SoUE9t9o7 | ⏳ 대기 |
| C3 | **GitHub repo 공개 확인** | github.com/noivan0/copy-perp — Public 설정 | ⏳ 대기 |
| C4 | **데모 영상 촬영 + YouTube 업로드** | 데모 스크립트 기준 (docs/demo-script.md) | ⏳ 대기 |

---

## 🟡 DEV 태스크 (Dev팀장 실행)

### Phase 1: 코드 완성 및 점검 (오늘 ~ 4/12)

| # | 태스크 | 파일 | 상태 |
|---|--------|------|------|
| D1 | README.md 해커톤 제출용 업그레이드 | copy-perp/README.md | ✅ 기존 있음 → 보완 필요 |
| D2 | copy-perp-web/README.md 업그레이드 | copy-perp-web/README.md | ❌ 기본 Next.js 템플릿 |
| D3 | 전체 테스트 PASS 확인 | tests/ | ⏳ 재실행 필요 |
| D4 | .env.example 최신화 | .env.example | ⏳ 확인 필요 |
| D5 | git push 최신화 | github.com/noivan0/copy-perp | ⏳ 확인 필요 |
| D6 | 프론트 빌드 확인 | copy-perp-web/ | ⏳ npm run build |
| D7 | API 헬스체크 확인 | /health, /healthz | ⏳ 서버 기동 테스트 |
| D8 | 데모 스크립트 최종 리허설 준비 | docs/demo-script.md | ⏳ 커맨드 최신화 |

### Phase 2: 문서 완성 (4/12 ~ 4/14)

| # | 태스크 | 내용 | 상태 |
|---|--------|------|------|
| D9 | README.md — 데모 영상 링크 삽입 | 영상 업로드 후 | ⏳ |
| D10 | README.md — 실제 체결 증거 스크린샷 | Order ID 296419238 | ⏳ |
| D11 | ARCHITECTURE.md 최신화 | 실제 구현과 동기화 | ⏳ |
| D12 | docs/demo-e2e-checklist.md CP#4 완료 표시 | 수동 완료 | ⏳ |

### Phase 3: 제출 준비 (4/14 ~ 4/16)

| # | 태스크 | 내용 | 상태 |
|---|--------|------|------|
| D13 | 제출 체크리스트 전항목 최종 확인 | QA_RELEASE_SCHEDULE.md CP#5 | ⏳ |
| D14 | git 최종 태그 | `git tag v1.0.0-hackathon` | ⏳ |

---

## 📋 제출 폼 필요 항목

Pacifica 제출 폼 (forms.gle/zYm9ZBH1SoUE9t9o7):

```
[ ] 팀명: Copy Perp
[ ] 트랙: Track 3 — Social & Gamification
[ ] GitHub URL: https://github.com/noivan0/copy-perp
[ ] 데모 영상 URL: [YouTube 링크]
[ ] 프로젝트 설명 (150자 이내)
[ ] 팀원 목록
[ ] Builder Code: noivan
[ ] 연락처
```

---

## ⏰ 일별 타임라인

```
4/10 (오늘) — D1~D8 완료, 테스트 재확인
4/11         — 프론트 최종 점검, D9~D12 문서 완성
4/12         — QA 최종 릴리즈 게이트 (54 PASS 확인)
4/13         — 데모 리허설 (노이반님 + Dev팀장)
4/14         — 데모 영상 촬영 + YouTube 업로드 (C4)
4/15         — README 영상 링크 삽입 (D9), git 최종 push
4/16 09:00  — 제출 폼 작성 + 제출 (C2) 🏁
```
