from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
import json
import time
import asyncio
import uuid
import websockets

app = FastAPI(title="ClawAPI", version="1.0.0")

GW_URL = "ws://127.0.0.1:18789"
GW_TOKEN = "20adebbab931a96b33579ce6b635fd86db66bb05d29a06a7"


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    agent: Optional[str] = "main"
    timeout: Optional[int] = 120


class ChatResponse(BaseModel):
    response: str
    session_id: str


async def gateway_chat(message: str, session_id: str, agent: str, timeout: int) -> str:
    async with websockets.connect(GW_URL, open_timeout=10) as ws:
        # 1. Challenge
        await asyncio.wait_for(ws.recv(), timeout=5)

        # 2. Connect
        await ws.send(json.dumps({
            "type": "req", "id": "1", "method": "connect",
            "params": {
                "minProtocol": 3, "maxProtocol": 3,
                "client": {
                    "id": "gateway-client",
                    "displayName": "ClawAPI",
                    "version": "1.0.0",
                    "platform": "linux",
                    "mode": "backend",
                },
                "caps": [],
                "auth": {"token": GW_TOKEN},
                "role": "operator",
                "scopes": ["operator.admin", "operator.read", "operator.write"],
            }
        }))
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        resp = json.loads(raw)
        if resp.get("type") == "res" and not resp.get("ok", True):
            raise Exception(f"Connect failed: {resp.get('error', {})}")

        # 3. Chat send
        session_key = f"agent:{agent}:{session_id}"
        await ws.send(json.dumps({
            "type": "req", "id": "2", "method": "chat.send",
            "params": {
                "sessionKey": session_key,
                "message": message,
                "idempotencyKey": str(uuid.uuid4()),
                "timeoutMs": timeout * 1000,
            }
        }))

        # Get the run ID from ack
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        ack = json.loads(raw)
        run_id = None
        if ack.get("type") == "res" and ack.get("id") == "2":
            payload = ack.get("payload", {})
            run_id = payload.get("runId")

        # 4. Listen for agent stream events
        text_parts = []
        deadline = time.time() + timeout

        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(15, deadline - time.time()))
                data = json.loads(raw)
                
                if data.get("type") != "event":
                    continue

                p = data.get("payload", {})
                event_name = data.get("event", "")
                
                # Agent stream events
                if event_name == "agent":
                    stream = p.get("stream", "")
                    d = p.get("data", "")
                    event_run = p.get("runId")
                    
                    # Only process events for our run
                    if run_id and event_run != run_id:
                        continue
                    
                    # Text stream = the assistant's reply
                    if stream == "text" and isinstance(d, str):
                        text_parts.append(d)
                    
                    # Lifecycle done/error
                    if stream == "lifecycle" and isinstance(d, dict):
                        phase = d.get("phase")
                        if phase == "done":
                            if text_parts:
                                return "".join(text_parts)
                            # Try to get reply from the done payload
                            return d.get("reply", "Agent completed without text output")
                        elif phase == "error":
                            raise Exception(d.get("error", "Agent error"))

                # Chat state events
                elif event_name == "chat":
                    state = p.get("state")
                    event_run = p.get("runId")
                    if run_id and event_run != run_id:
                        continue
                    if state == "idle" and text_parts:
                        return "".join(text_parts)
                    elif state == "error":
                        err = p.get("errorMessage", "Unknown error")
                        raise Exception(f"Chat error: {err}")

            except asyncio.TimeoutError:
                continue

        if text_parts:
            return "".join(text_parts)
        raise Exception("Agent response timeout")


@app.get("/")
def root():
    return {"status": "ok", "service": "ClawAPI"}


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    session_id = req.session_id or f"clawapi-{int(time.time() * 1000)}"
    try:
        response = await gateway_chat(
            message=req.message, session_id=session_id,
            agent=req.agent, timeout=req.timeout,
        )
        return ChatResponse(response=response, session_id=session_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
