"""External provider/SMTP fakes on the isolated Docker network, not auth mocks."""

import asyncio
import hashlib
import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request

MAIL: dict[str, list[str]] = {}
EGRESS: list[str] = []


async def smtp(reader, writer):
    """Accept SMTP used by the real application email adapter without delivery."""
    recipient = ""
    writer.write(b"220 tenancy fixture\r\n")
    await writer.drain()
    try:
        while line := await reader.readline():
            command = line.decode().strip()
            if command.upper().startswith("RCPT TO:"):
                recipient = command.split(":", 1)[1].strip("<>").lower()
            if command.upper() == "DATA":
                writer.write(b"354 finish with dot\r\n")
                await writer.drain()
                body = []
                while (line := await reader.readline()) not in {b".\r\n", b""}:
                    body.append(line.decode())
                MAIL.setdefault(recipient, []).append("".join(body))
                save_mailbox(recipient)
            if command.upper() == "QUIT":
                writer.write(b"221 bye\r\n")
                break
            writer.write(b"250 ok\r\n")
            await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


def save_mailbox(recipient):
    """Mirror synthetic email proof into private test storage, never CI artifacts."""
    root = Path(os.environ["TENANCY_MAIL_DIR"])
    path = root / (hashlib.sha256(recipient.encode()).hexdigest() + ".json")
    value = mail(recipient)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.chmod(0o600)
    temporary.replace(path)


@asynccontextmanager
async def lifespan(app):
    """Open an internal-only SMTP listener alongside HTTP provider fixtures."""
    server = await asyncio.start_server(smtp, "0.0.0.0", 1025)
    yield
    server.close()
    await server.wait_closed()


app = FastAPI(lifespan=lifespan)


@app.api_route("/live/anthropic/v1/{endpoint}", methods=["GET", "POST"])
async def live_anthropic(request: Request):
    """Enable real model access only with the explicit budgeted live overlay."""
    if "TENANCY_LIVE_CONFIG" not in os.environ:
        from fastapi import HTTPException

        raise HTTPException(404, "Live testing is not enabled")
    from tenancy_harness.live_relay import relay

    return await relay(request)


@app.get("/health")
def health():
    """Identify the fake without requiring any external service."""
    return {"fixture": "tenancy-phase-0"}


@app.get("/mail/{recipient}")
def mail(recipient: str):
    """Expose only synthetic mailbox proof to the private fixture process."""
    messages = MAIL.get(recipient.lower(), [])
    matches = re.findall(r"\b\d{6}\b", messages[-1] if messages else "")
    return {"count": len(messages), "code": matches[0] if matches else None}


@app.get("/v1/models")
@app.get("/egress-probe/v1/models")
def models(request: Request):
    """Record isolated outbound dispatch and advertise one synthetic model."""
    EGRESS.append(request.url.path)
    return {"data": [{"id": "tenancy-fixture-model", "owned_by": "fixture"}]}


@app.get("/egress")
def egress():
    """Return destination paths only, never headers or submitted credentials."""
    return {"paths": EGRESS}


@app.post("/v1/chat/completions")
async def completion(request: Request):
    """Return recognizable tenant content through the actual model adapter."""
    body = await request.json()
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
