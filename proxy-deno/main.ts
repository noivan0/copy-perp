// Pacifica API 프록시 — Deno Deploy용
// POST 포함 모든 메서드 지원

const PACIFICA_BASE = Deno.env.get("PACIFICA_BASE") ?? "https://test-api.pacifica.fi";

Deno.serve(async (req: Request) => {
  const url = new URL(req.url);

  // CORS preflight
  if (req.method === "OPTIONS") {
    return new Response(null, {
      headers: {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
      },
    });
  }

  if (url.pathname === "/health") {
    return Response.json({ status: "ok", target: PACIFICA_BASE });
  }

  // /api/v1/... → Pacifica로 프록시
  const targetUrl = `${PACIFICA_BASE}${url.pathname}${url.search}`;

  const headers = new Headers(req.headers);
  headers.set("host", new URL(PACIFICA_BASE).host);
  headers.delete("x-forwarded-for");

  try {
    const resp = await fetch(targetUrl, {
      method: req.method,
      headers,
      body: req.method !== "GET" && req.method !== "HEAD" ? req.body : null,
    });

    const respHeaders = new Headers(resp.headers);
    respHeaders.set("Access-Control-Allow-Origin", "*");

    return new Response(resp.body, {
      status: resp.status,
      headers: respHeaders,
    });
  } catch (e) {
    return Response.json({ error: String(e) }, { status: 500 });
  }
});
