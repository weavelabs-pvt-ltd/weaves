"""External provider fakes (models, MCP, Google) on the isolated Docker network."""

import asyncio
import contextlib
import hashlib
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request

from tenancy_harness.fake_google import router as google_router

EGRESS: list[str] = []
MODEL_EVENTS: list[dict[str, str]] = []


class PlainServer(uvicorn.Server):
    """A second listener inside this process; the primary server owns signals."""

    @contextlib.contextmanager
    def capture_signals(self):
        """Leave signal handling to the TLS server."""
        yield


@asynccontextmanager
async def lifespan(app):
    """Serve the same fakes over plain HTTP for the Google endpoints on 8081.

    Google discovery must be plain HTTP because its authorization endpoint is the
    harness reverse proxy, which is reachable by the browser only over HTTP.
    """
    config = uvicorn.Config(
        app, host="0.0.0.0", port=8081, log_level="warning", lifespan="off"
    )
    server = PlainServer(config)
    task = asyncio.create_task(server.serve())
    while not server.started and not task.done():
        await asyncio.sleep(0.05)
    yield
    server.should_exit = True
    await task


app = FastAPI(lifespan=lifespan)
app.include_router(google_router)


@app.api_route("/v1/messages", methods=["POST"])
async def live_messages(request: Request):
    """Relay only on the explicit overlay, through its separate budget service."""
    return await forward_live(request, "messages")


async def forward_live(request, endpoint):
    """The relay's network has no fake canonical-host aliases."""
    import httpx
    from fastapi import HTTPException
    from starlette.responses import Response

    relay_url = os.environ.get("TENANCY_LIVE_RELAY_URL")
    if not relay_url:
        raise HTTPException(404, "Live testing is not enabled")
    async with httpx.AsyncClient(timeout=45, trust_env=False) as client:
        response = await client.request(
            request.method,
            relay_url + "/v1/" + endpoint,
            content=await request.body(),
            headers={
                "x-api-key": request.headers.get("x-api-key", ""),
                "content-type": "application/json",
            },
        )
    return Response(
        response.content,
        status_code=response.status_code,
        media_type="application/json",
        headers={"request-id": response.headers.get("request-id", "")},
    )


@app.get("/health")
def health():
    """Identify the fake without requiring any external service."""
    return {"fixture": "tenancy-phase-0"}


@app.get("/v1/models")
@app.get("/egress-probe/v1/models")
async def models(request: Request):
    """Record isolated outbound dispatch and advertise one synthetic model."""
    if request.headers.get("host") == "api.anthropic.com" and os.environ.get(
        "TENANCY_LIVE_RELAY_URL"
    ):
        return await forward_live(request, "models")
    EGRESS.append(request.url.path)
    MODEL_EVENTS.append(
        {
            "path": request.url.path,
            "host": request.headers.get("host", ""),
            "credential_sha256": hashlib.sha256(
                request.headers.get("authorization", "").encode()
            ).hexdigest(),
        }
    )
    return {"data": [{"id": "tenancy-fixture-model", "owned_by": "fixture"}]}


@app.get("/redirect/v1/models")
def redirect_models():
    """Redirect to a private probe; clients must never follow it."""
    from starlette.responses import RedirectResponse

    EGRESS.append("/redirect/v1/models")
    return RedirectResponse("https://provider/egress-probe/v1/models", status_code=307)


@app.get("/model-events")
def model_events():
    """Expose synthetic authentication fingerprints only on the private fixture."""
    return {"events": MODEL_EVENTS}


@app.get("/egress")
def egress():
    """Return destination paths only, never headers or submitted credentials."""
    return {"paths": EGRESS}


@app.post("/v1/chat/completions")
async def completion(request: Request):
    """Return recognizable tenant content through the actual model adapter."""
    body = await request.json()
    MODEL_EVENTS.append(
        {
            "path": request.url.path,
            "host": request.headers.get("host", ""),
            "credential_sha256": hashlib.sha256(
                request.headers.get("authorization", "").encode()
            ).hexdigest(),
        }
    )
    prompt = body["messages"][-1]["content"]
    return {
        "id": "fixture-completion",
        "model": "tenancy-fixture-model",
        "choices": [{"message": {"role": "assistant", "content": prompt}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    }


@app.post("/mcp")
async def mcp(request: Request):
    """Serve a real HTTP MCP handshake and a tenant-labelled read-only tool."""
    body = await request.json()
    marker = request.headers.get("authorization", "").split()[-1]
    method = body["method"]
    if method == "notifications/initialized":
        from fastapi import Response

        return Response(status_code=202)
    result = mcp_result(method, marker)
    return {"jsonrpc": "2.0", "id": body["id"], "result": result}


def mcp_result(method, marker):
    """Describe synthetic tools without replacing product installation checks."""
    if method == "initialize":
        return {"protocolVersion": "2026-07-28", "capabilities": {}}
    if method == "tools/list":
        return {
            "resultType": "complete",
            "tools": [
                {
                    "name": "search_docs",
                    "description": marker + " private schema",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"query": {"type": "string", "minLength": 1}},
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                }
            ],
        }
    return {
        "resultType": "complete",
        "content": [{"type": "text", "text": marker + " private context"}],
    }
