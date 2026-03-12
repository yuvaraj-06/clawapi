from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List
import json
import time
import asyncio
import uuid
import websockets
from datetime import datetime, timezone
from pymongo import MongoClient

app = FastAPI(title="ClawAPI", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

GW_URL = "ws://127.0.0.1:18789"
GW_TOKEN = "20adebbab931a96b33579ce6b635fd86db66bb05d29a06a7"
MONGO_URI = "mongodb+srv://yuvaraj:Yc7aNShY2Cpbj5D2@shareos.ekz2onb.mongodb.net/?retryWrites=true&w=majority&appName=shareos"

# Request queue for rate limit handling
_request_semaphore = asyncio.Semaphore(4)  # Max 4 concurrent gateway requests
_retry_delays = [2, 4, 8, 15, 30]  # Exponential backoff seconds

# MongoDB
_mongo_client = None
_mongo_db = None

def get_db():
    global _mongo_client, _mongo_db
    if _mongo_db is None:
        _mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        _mongo_db = _mongo_client.get_database("shareos")
        col = _mongo_db.clawos_app_chat_history
        col.create_index("session_id")
        col.create_index([("session_id", 1), ("timestamp", 1)])
    return _mongo_db


def save_message(session_id: str, role: str, content: str, model: str = None):
    try:
        db = get_db()
        db.clawos_app_chat_history.insert_one({
            "session_id": session_id,
            "role": role,
            "content": content,
            "model": model,
            "timestamp": datetime.now(timezone.utc),
            "date_id": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        })
    except Exception:
        pass


def get_recent_history(session_id: str, limit: int = 20) -> List[dict]:
    try:
        db = get_db()
        msgs = list(db.clawos_app_chat_history.find(
            {"session_id": session_id},
            {"_id": 0, "role": 1, "content": 1, "timestamp": 1}
        ).sort("timestamp", -1).limit(limit))
        msgs.reverse()
        return msgs
    except Exception:
        return []


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    agent: Optional[str] = "main"
    timeout: Optional[int] = 120
    stream: Optional[bool] = False


class ChatResponse(BaseModel):
    response: str
    session_id: str


async def _connect_gateway():
    ws = await websockets.connect(GW_URL, open_timeout=10)
    await asyncio.wait_for(ws.recv(), timeout=5)
    await ws.send(json.dumps({
        "type": "req", "id": "1", "method": "connect",
        "params": {
            "minProtocol": 3, "maxProtocol": 3,
            "client": {"id": "gateway-client", "displayName": "ClawAPI Channel", "version": "2.0.0", "platform": "linux", "mode": "backend"},
            "caps": [], "auth": {"token": GW_TOKEN},
            "role": "operator", "scopes": ["operator.admin", "operator.read", "operator.write"],
        }
    }))
    raw = await asyncio.wait_for(ws.recv(), timeout=10)
    resp = json.loads(raw)
    if resp.get("type") == "res" and not resp.get("ok", True):
        await ws.close()
        raise Exception(f"Connect failed: {resp.get('error', {})}")
    return ws


async def _send_chat(ws, session_key, message, timeout):
    rid = str(uuid.uuid4())
    await ws.send(json.dumps({
        "type": "req", "id": "2", "method": "chat.send",
        "params": {"sessionKey": session_key, "message": message, "idempotencyKey": rid, "timeoutMs": timeout * 1000}
    }))
    raw = await asyncio.wait_for(ws.recv(), timeout=10)
    ack = json.loads(raw)
    if ack.get("type") == "res" and ack.get("id") == "2":
        if not ack.get("ok", True):
            err = ack.get("error", {})
            raise Exception(err.get("message", str(err)))
        return ack.get("payload", {}).get("runId")
    return None


def _extract_text(stream_name, data):
    if stream_name == "text" and isinstance(data, str):
        return data
    if stream_name == "assistant" and isinstance(data, dict):
        return data.get("delta", "") or ""
    return ""


async def _collect_response(ws, run_id, timeout):
    """Collect full response from gateway, handling rate limit retries internally."""
    text_parts = []
    deadline = time.time() + timeout
    error_count = 0

    while time.time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=min(15, deadline - time.time()))
            data = json.loads(raw)
            if data.get("type") != "event":
                continue
            p = data.get("payload", {})

            if data.get("event") == "agent":
                stream = p.get("stream", "")
                d = p.get("data", "")
                txt = _extract_text(stream, d)
                if txt:
                    text_parts.append(txt)
                    error_count = 0
                if stream == "lifecycle" and isinstance(d, dict):
                    phase = d.get("phase")
                    if phase in ("done", "end") and text_parts:
                        return "".join(text_parts), None
                    if phase == "error":
                        if text_parts:
                            return "".join(text_parts), None
                        error_msg = d.get("error", "Agent error")
                        if "rate limit" in error_msg.lower():
                            error_count += 1
                            if error_count >= 8:
                                return None, error_msg
                            continue  # Wait for gateway auto-retry
                        else:
                            return None, error_msg

            elif data.get("event") == "chat":
                state = p.get("state")
                if state in ("idle", "final") and text_parts:
                    return "".join(text_parts), None
                elif state == "error" and not text_parts:
                    return None, p.get("errorMessage", "Chat error")

        except asyncio.TimeoutError:
            if text_parts:
                return "".join(text_parts), None
            continue

    return ("".join(text_parts) if text_parts else None), "Timeout"


async def gateway_chat_with_retry(message, session_id, agent, timeout):
    """Send chat with queuing and full retry logic. Never exposes rate limit errors to users."""
    last_error = None

    for attempt in range(len(_retry_delays) + 1):
        if attempt > 0:
            delay = _retry_delays[min(attempt - 1, len(_retry_delays) - 1)]
            await asyncio.sleep(delay)

        try:
            async with _request_semaphore:
                ws = await _connect_gateway()
                try:
                    session_key = f"agent:{agent}:{session_id}"
                    run_id = await _send_chat(ws, session_key, message, timeout)
                    result, error = await _collect_response(ws, run_id, timeout)

                    if result:
                        return result
                    if error and "rate limit" not in error.lower():
                        raise Exception(error)
                    last_error = error
                finally:
                    await ws.close()
        except Exception as e:
            err_str = str(e).lower()
            if "rate limit" in err_str:
                last_error = str(e)
                continue
            raise

    raise Exception(last_error or "All retries exhausted")


async def gateway_stream_with_retry(message, session_id, agent, timeout):
    """Stream chat with retry. If first attempt hits rate limit, retries before streaming."""
    last_error = None

    for attempt in range(len(_retry_delays) + 1):
        if attempt > 0:
            delay = _retry_delays[min(attempt - 1, len(_retry_delays) - 1)]
            await asyncio.sleep(delay)

        try:
            async with _request_semaphore:
                ws = await _connect_gateway()
                try:
                    session_key = f"agent:{agent}:{session_id}"
                    run_id = await _send_chat(ws, session_key, message, timeout)

                    deadline = time.time() + timeout
                    has_text = False
                    error_count = 0
                    all_text = []

                    while time.time() < deadline:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=min(15, deadline - time.time()))
                            data = json.loads(raw)
                            if data.get("type") != "event":
                                continue
                            p = data.get("payload", {})

                            if data.get("event") == "agent":
                                stream = p.get("stream", "")
                                d = p.get("data", "")
                                txt = _extract_text(stream, d)
                                if txt:
                                    has_text = True
                                    error_count = 0
                                    all_text.append(txt)
                                    yield f"data: {json.dumps({'type': 'text', 'content': txt})}\n\n"
                                if stream == "lifecycle" and isinstance(d, dict):
                                    phase = d.get("phase")
                                    if phase in ("done", "end"):
                                        if all_text:
                                            save_message(session_id, "assistant", "".join(all_text), agent)
                                        yield f"data: {json.dumps({'type': 'done'})}\n\n"
                                        return
                                    if phase == "error":
                                        err_msg = d.get("error", "")
                                        if has_text:
                                            save_message(session_id, "assistant", "".join(all_text), agent)
                                            yield f"data: {json.dumps({'type': 'done'})}\n\n"
                                            return
                                        if "rate limit" in err_msg.lower():
                                            error_count += 1
                                            if error_count >= 8:
                                                # Break inner loop, retry outer
                                                last_error = err_msg
                                                break
                                            continue  # Wait for gateway retry
                                        else:
                                            yield f"data: {json.dumps({'type': 'error', 'message': err_msg})}\n\n"
                                            return

                            elif data.get("event") == "chat":
                                state = p.get("state")
                                if state in ("idle", "final"):
                                    if all_text:
                                        save_message(session_id, "assistant", "".join(all_text), agent)
                                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                                    return
                                elif state == "error" and not has_text:
                                    err_msg = p.get("errorMessage", "")
                                    if "rate limit" in err_msg.lower():
                                        last_error = err_msg
                                        break
                                    yield f"data: {json.dumps({'type': 'error', 'message': err_msg})}\n\n"
                                    return

                        except asyncio.TimeoutError:
                            if has_text:
                                save_message(session_id, "assistant", "".join(all_text), agent)
                                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                                return
                            continue

                    # If we got text but broke out, still return what we have
                    if has_text and all_text:
                        save_message(session_id, "assistant", "".join(all_text), agent)
                        yield f"data: {json.dumps({'type': 'done'})}\n\n"
                        return

                finally:
                    await ws.close()

        except Exception as e:
            if "rate limit" in str(e).lower():
                last_error = str(e)
                continue
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
            return

    # All retries failed
    yield f"data: {json.dumps({'type': 'error', 'message': 'Service temporarily busy. Please try again in a moment.'})}\n\n"


@app.get("/")
def root():
    return {"status": "ok", "service": "ClawAPI Channel", "version": "2.0.0"}

@app.get("/health")
def health():
    return {"status": "healthy"}

@app.post("/chat")
async def chat(req: ChatRequest):
    session_id = req.session_id or f"talk-{uuid.uuid4()}"

    # Save user message
    save_message(session_id, "user", req.message)

    if req.stream:
        return StreamingResponse(
            gateway_stream_with_retry(req.message, session_id, req.agent, req.timeout),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Session-Id": session_id},
        )

    try:
        response = await gateway_chat_with_retry(req.message, session_id, req.agent, req.timeout)
        save_message(session_id, "assistant", response, req.agent)
        return ChatResponse(response=response, session_id=session_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/history/{session_id}")
def chat_history_get(session_id: str, limit: int = 50):
    msgs = get_recent_history(session_id, limit)
    return {"session_id": session_id, "messages": msgs}


# Context-aware chat endpoint (like Notion AI)
class ContextChatRequest(BaseModel):
    message: str
    context: dict  # Full page context including API responses, current state, etc.
    session_id: Optional[str] = None
    agent: Optional[str] = "main"
    timeout: Optional[int] = 120
    stream: Optional[bool] = False


class ContextChatResponse(BaseModel):
    response: str
    session_id: str
    context_used: dict  # Echo back the context that was used


def _build_context_prompt(context: dict) -> str:
    """Build a system prompt from the page context."""
    context_parts = []
    
    # Current page info
    if "page" in context:
        context_parts.append(f"Current Page: {context['page']}")
    if "route" in context:
        context_parts.append(f"Route: {context['route']}")
    
    # Company/venture info
    if "company" in context:
        context_parts.append(f"\nCompany: {json.dumps(context['company'], indent=2)}")
    
    # Tab/view info
    if "tab" in context:
        context_parts.append(f"\nCurrent Tab: {context['tab']}")
    
    # API data from the page
    if "api_data" in context:
        context_parts.append(f"\nAPI Data:\n{json.dumps(context['api_data'], indent=2)}")
    
    # Workstreams data
    if "workstreams" in context:
        context_parts.append(f"\nWorkstreams:\n{json.dumps(context['workstreams'], indent=2)}")
    
    # Goals/Milestones/Tasks
    if "goals" in context:
        context_parts.append(f"\nGoals:\n{json.dumps(context['goals'], indent=2)}")
    if "milestones" in context:
        context_parts.append(f"\nMilestones:\n{json.dumps(context['milestones'], indent=2)}")
    if "tasks" in context:
        context_parts.append(f"\nTasks:\n{json.dumps(context['tasks'], indent=2)}")
    
    # Updates
    if "updates" in context:
        context_parts.append(f"\nUpdates:\n{json.dumps(context['updates'], indent=2)}")
    
    # Any additional context
    if "additional" in context:
        context_parts.append(f"\nAdditional Context:\n{json.dumps(context['additional'], indent=2)}")
    
    return "\n".join(context_parts)


@app.post("/chat/context")
async def context_chat(req: ContextChatRequest):
    """
    Context-aware chat endpoint.
    Each request creates a NEW session to keep conversations isolated.
    The context from the current page is passed to the agent.
    """
    # Create a NEW session for this chat (isolation between chats)
    session_id = req.session_id or f"context-{uuid.uuid4()}"
    
    # Build context prompt
    context_prompt = _build_context_prompt(req.context)
    
    # Create system message with context
    system_msg = f"""You are an AI assistant helping a user navigate a ShareOS venture intelligence platform.
The user is currently viewing a page with the following context:

{context_prompt}

Based on this context, answer the user's question. If the context doesn't contain enough information to answer, acknowledge that and provide your best answer based on what you know about ShareOS ventures."""

    # Build the full message with context
    full_message = f"{system_msg}\n\nUser Question: {req.message}"
    
    # Save user message with context reference
    save_message(session_id, "user", req.message)
    
    if req.stream:
        return StreamingResponse(
            gateway_stream_with_retry(full_message, session_id, req.agent, req.timeout),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Session-Id": session_id},
        )

    try:
        response = await gateway_chat_with_retry(full_message, session_id, req.agent, req.timeout)
        save_message(session_id, "assistant", response, req.agent)
        return ContextChatResponse(
            response=response, 
            session_id=session_id,
            context_used={
                "page": req.context.get("page"),
                "route": req.context.get("route"),
                "tab": req.context.get("tab"),
                "company": req.context.get("company", {}).get("name") if isinstance(req.context.get("company"), dict) else req.context.get("company")
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
