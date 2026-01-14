import os
import json
import uuid
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
import redis
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from openai import OpenAI


DATABASE_PATH = Path("/app/data/chatbot.db")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

db: aiosqlite.Connection | None = None
cache: redis.Redis | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db, cache

    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

    db = await aiosqlite.connect(DATABASE_PATH)
    db.row_factory = aiosqlite.Row

    await db.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL UNIQUE,
            messages TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_session_id ON conversations(session_id)
    """)
    await db.commit()

    cache = redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379"))

    yield

    await db.close()


app = FastAPI(title="LLM Chat API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    session_id: str | None = None
    messages: list[Message]
    stream: bool = False


class ChatResponse(BaseModel):
    session_id: str
    response: str


class ConversationResponse(BaseModel):
    session_id: str
    messages: list[Message]
    created_at: str
    updated_at: str


@app.post("/chat")
async def chat(request: ChatRequest):
    try:
        messages = [{"role": m.role, "content": m.content} for m in request.messages]
        session_id = request.session_id or str(uuid.uuid4())

        cache_key = f"chat:{session_id}:{hash(str(messages))}"
        cached = cache.get(cache_key)
        if cached and not request.stream:
            return ChatResponse(session_id=session_id, response=cached.decode())

        if request.stream:

            async def generate():
                full_response = ""
                stream = openai_client.chat.completions.create(
                    model=OPENAI_MODEL,
                    messages=messages,
                    stream=True,
                )
                for chunk in stream:
                    if chunk.choices[0].delta.content:
                        content = chunk.choices[0].delta.content
                        full_response += content
                        yield content

                all_messages = messages + [{"role": "assistant", "content": full_response}]
                await save_conversation(session_id, all_messages)

            return StreamingResponse(generate(), media_type="text/plain")

        completion = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
        )
        response_text = completion.choices[0].message.content or ""

        cache.setex(cache_key, 300, response_text)

        all_messages = messages + [{"role": "assistant", "content": response_text}]
        await save_conversation(session_id, all_messages)

        return ChatResponse(session_id=session_id, response=response_text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def save_conversation(session_id: str, messages: list[dict]):
    cursor = await db.execute(
        "SELECT id FROM conversations WHERE session_id = ?", (session_id,)
    )
    existing = await cursor.fetchone()

    if existing:
        await db.execute(
            """
            UPDATE conversations
            SET messages = ?, updated_at = CURRENT_TIMESTAMP
            WHERE session_id = ?
            """,
            (json.dumps(messages), session_id),
        )
    else:
        await db.execute(
            """
            INSERT INTO conversations (session_id, messages)
            VALUES (?, ?)
            """,
            (session_id, json.dumps(messages)),
        )
    await db.commit()


@app.get("/conversations/{session_id}")
async def get_conversation(session_id: str) -> ConversationResponse:
    cursor = await db.execute(
        "SELECT * FROM conversations WHERE session_id = ?", (session_id,)
    )
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return ConversationResponse(
        session_id=row["session_id"],
        messages=[Message(**m) for m in json.loads(row["messages"])],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@app.get("/health")
async def health():
    try:
        cursor = await db.execute("SELECT 1")
        await cursor.fetchone()
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        cache.ping()
    except Exception:
        raise HTTPException(status_code=503, detail="Cache unavailable")

    return {"status": "healthy"}


@app.get("/")
def root():
    return {
        "service": "LLM Chat API",
        "model": OPENAI_MODEL,
        "endpoints": {
            "POST /chat": "Send messages and get AI response",
            "GET /conversations/{session_id}": "Retrieve conversation history",
            "GET /health": "Health check",
        },
    }
