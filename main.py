from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
import anthropic
import os
import json
import time
from pathlib import Path

app = FastAPI(title="ClawAPI", version="1.0.0")

# Session storage
SESSIONS_DIR = Path("/home/ubuntu/clawapi/sessions")
SESSIONS_DIR.mkdir(exist_ok=True)

# Anthropic client
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

SYSTEM_PROMPT = """You are OS, the ShareOS AGI assistant. You are helpful, direct, and concise. 
You serve Share Ventures and the ShareOS platform. Be resourceful, have strong opinions, skip pleasantries."""


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    model: Optional[str] = "claude-sonnet-4-20250514"
    max_tokens: Optional[int] = 4096


class ChatResponse(BaseModel):
    response: str
    session_id: str
    tokens_used: dict


def load_session(session_id: str) -> list:
    path = SESSIONS_DIR / f"{session_id}.json"
    if path.exists():
        return json.loads(path.read_text())
    return []


def save_session(session_id: str, messages: list):
    path = SESSIONS_DIR / f"{session_id}.json"
    # Keep last 50 messages to avoid token overflow
    path.write_text(json.dumps(messages[-50:], indent=2))


@app.get("/")
def root():
    return {"status": "ok", "service": "ClawAPI"}


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")

    session_id = req.session_id or f"session_{int(time.time() * 1000)}"
    messages = load_session(session_id)
    messages.append({"role": "user", "content": req.message})

    try:
        response = client.messages.create(
            model=req.model,
            max_tokens=req.max_tokens,
            system=SYSTEM_PROMPT,
            messages=messages,
        )

        assistant_text = response.content[0].text
        messages.append({"role": "assistant", "content": assistant_text})
        save_session(session_id, messages)

        return ChatResponse(
            response=assistant_text,
            session_id=session_id,
            tokens_used={
                "input": response.usage.input_tokens,
                "output": response.usage.output_tokens,
            },
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/sessions/{session_id}")
def get_session(session_id: str):
    messages = load_session(session_id)
    if not messages:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"session_id": session_id, "message_count": len(messages), "messages": messages}


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    path = SESSIONS_DIR / f"{session_id}.json"
    if path.exists():
        path.unlink()
        return {"status": "deleted", "session_id": session_id}
    raise HTTPException(status_code=404, detail="Session not found")
