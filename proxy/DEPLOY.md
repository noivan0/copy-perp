# Pacifica API 프록시 - render.com 배포

## 1분 배포 가이드

1. https://render.com 접속 → GitHub 로그인
2. "New +" → "Web Service"
3. `noivan0/copy-perp` 선택
4. 설정:
   - **Root Directory**: `proxy`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. "Create Web Service" → 3분 빌드
6. URL 받으면 (예: `https://copy-perp-proxy.onrender.com`)

## .env 수정

```
PACIFICA_REST_URL=https://copy-perp-proxy.onrender.com
```

## 완료

sandbox에서 `python3 scripts/test_api_connection.py` 실행하면
render.com → Pacifica 테스트넷으로 프록시되어 실제 API 연결됩니다.
