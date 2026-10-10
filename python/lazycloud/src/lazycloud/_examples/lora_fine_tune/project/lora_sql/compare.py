"""An endpoint that answers one question with the base model and the adapter, side by side."""

import asyncio
import os
from typing import Annotated

from lazycloud import Secret
from lazycloud.schema import ValidationError
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from .data import MAX_CONTEXT_CHARS, MAX_QUESTION_CHARS
from .prompt import chat_messages, extract_sql
from .queries import QueryError, QueryRows, SchemaError, run_query
from .resources import BASE_MODEL_NAME, MAX_SQL_TOKENS, TUNED_MODEL_NAME, app, cpu_image

# The sql-server pod's URL, and an access token the pod accepts.
server_url = Secret("SQL_SERVER_URL")
server_token = Secret("SQL_SERVER_TOKEN")

# A cold sql-server loads the model before it answers its first request.
SERVER_TIMEOUT_SECONDS = 240

Question = Annotated[str, Field(min_length=1, max_length=MAX_QUESTION_CHARS)]
Context = Annotated[str, Field(min_length=1, max_length=MAX_CONTEXT_CHARS)]


class Answer(BaseModel):
    sql: str
    result: QueryRows | None
    error: str | None


class Comparison(BaseModel):
    question: str
    base: Answer
    tuned: Answer


@app.endpoint(
    name="compare",
    route="/compare",
    methods=["POST"],
    image=cpu_image,
    cpu=0.25,
    memory="256Mi",
    concurrency=8,
    keep_warm=120,
    timeout_seconds=300,
    secrets=[server_url.name, server_token.name],
)
async def compare(question: Question, context: Context) -> Comparison:
    """Ask both models, then run each query against the database `context` creates."""
    try:
        run_query(context, "SELECT 1")
    except SchemaError as exc:
        raise ValidationError(f"the database statements fail: {exc}", field="context") from exc
    async with AsyncOpenAI(
        base_url=f"{os.environ[server_url.name].rstrip('/')}/v1",
        api_key=os.environ[server_token.name],
        timeout=SERVER_TIMEOUT_SECONDS,
        max_retries=1,
    ) as client:
        base, tuned = await asyncio.gather(
            _answer(client, BASE_MODEL_NAME, context, question),
            _answer(client, TUNED_MODEL_NAME, context, question),
        )
    return Comparison(question=question, base=base, tuned=tuned)


async def _answer(client: AsyncOpenAI, model: str, context: str, question: str) -> Answer:
    completion = await client.chat.completions.create(
        model=model,
        messages=chat_messages(context, question),
        temperature=0,
        max_tokens=MAX_SQL_TOKENS,
    )
    sql = extract_sql(completion.choices[0].message.content or "")
    try:
        return Answer(sql=sql, result=run_query(context, sql), error=None)
    except QueryError as exc:
        return Answer(sql=sql, result=None, error=str(exc))
