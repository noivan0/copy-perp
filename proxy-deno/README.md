# Pacifica POST 프록시 — Deno Deploy

## 배포 (2분)

1. https://dash.deno.com → GitHub 로그인
2. "New Project" → `noivan0/copy-perp` 선택
3. Entry point: `proxy-deno/main.ts`
4. 배포 → URL 받기 (예: `https://pacifica-proxy.deno.dev`)

## .env 설정

```
PACIFICA_PROXY_URL=https://pacifica-proxy.deno.dev
```

## 테스트

```bash
curl https://pacifica-proxy.deno.dev/health
curl https://pacifica-proxy.deno.dev/api/v1/info
```
