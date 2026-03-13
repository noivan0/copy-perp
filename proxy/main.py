"""Pacifica API 프록시 서버 — GET + POST 모두 지원
render.com에 배포하면 HMG 웹필터 우회 가능
"""
import os, json, time
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

PACIFICA_BASE = os.getenv("PACIFICA_BASE", "https://test-api.pacifica.fi")

@app.get("/health")
def health():
    return {"status": "ok", "target": PACIFICA_BASE}

@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy(path: str, request: Request):
    target_url = f"{PACIFICA_BASE}/api/{path}"
    
    # 쿼리 파라미터 전달
    params = dict(request.query_params)
    
    # 요청 본문
    body = await request.body()
    
    # 헤더 전달 (host 제외)
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "transfer-encoding")
    }
    headers["host"] = PACIFICA_BASE.replace("https://", "").replace("http://", "")

    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        resp = await client.request(
            method=request.method,
            url=target_url,
            params=params,
            content=body if body else None,
            headers=headers,
        )
    
    # 응답 반환
    excluded_headers = {"transfer-encoding", "connection", "content-encoding"}
    resp_headers = {
        k: v for k, v in resp.headers.items()
        if k.lower() not in excluded_headers
    }
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=resp_headers,
        media_type=resp.headers.get("content-type", "application/json"),
    )
