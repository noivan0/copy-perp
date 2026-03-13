"""
Pacifica API Proxy Server
HMG Corp 방화벽 우회용 - render.com에 배포
"""
import os
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

PACIFICA_BASE = os.getenv("PACIFICA_BASE", "https://test-api.pacifica.fi/api/v1")

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(path: str, request: Request):
    url = f"{PACIFICA_BASE}/{path}"
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() 
               if k.lower() not in ("host", "content-length")}
    
    async with httpx.AsyncClient(timeout=30, verify=False) as client:
        resp = await client.request(
            method=request.method,
            url=url,
            content=body,
            headers=headers,
            params=dict(request.query_params),
        )
    return Response(content=resp.content, status_code=resp.status_code,
                    headers=dict(resp.headers))

@app.get("/health")
async def health():
    return {"ok": True, "proxy_target": PACIFICA_BASE}
