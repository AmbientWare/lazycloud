"""The chat prompt the model sees in training, evaluation and serving, and how its reply is read."""

import re
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openai.types.chat import ChatCompletionMessageParam

SYSTEM_PROMPT = (
    "You write SQLite queries. Given a database and a question, "
    "reply with one SQL query that answers the question and nothing else."
)

_FENCED = re.compile(r"```(?:sql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def chat_messages(context: str, question: str) -> "list[ChatCompletionMessageParam]":
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Database:\n{context}\n\nQuestion: {question}"},
    ]


def extract_sql(reply: str) -> str:
    """The first SQL statement in a reply, without Markdown fences or explanation."""
    fenced = _FENCED.search(reply)
    text = (fenced.group(1) if fenced else reply).strip()
    # complete_statement skips semicolons inside string literals.
    for end, character in enumerate(text, start=1):
        if character == ";" and sqlite3.complete_statement(text[:end]):
            return text[:end]
    return text
