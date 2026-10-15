"""The HTTP route GitHub delivers issue events to."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from lazycloud.exceptions import SdkError
from pydantic import BaseModel, ValidationError

from coding_agents.github_events import IssueJob, issue_job, signature_matches

MAX_DELIVERY_BYTES = 1_000_000


class Delivery(BaseModel):
    status: Literal["pong", "ignored", "started", "already_running"]
    task_id: str | None = None


def create_api(start: Callable[[IssueJob], str | None], *, secret_name: str) -> FastAPI:
    """start begins work on a job and returns its task ID, or None if it is already running.

    secret_name is the environment variable holding the webhook secret set in GitHub.
    """
    api = FastAPI(title="Coding agents webhook")

    @api.post("/github")
    async def github_delivery(
        request: Request,
        x_github_event: Annotated[str, Header()],
        x_hub_signature_256: Annotated[str | None, Header()] = None,
    ) -> Delivery:
        body = await request.body()
        if len(body) > MAX_DELIVERY_BYTES:
            raise HTTPException(413, "delivery too large")
        secret = os.environ[secret_name].encode()
        if not signature_matches(secret, body, x_hub_signature_256):
            raise HTTPException(401, "signature does not match")
        if x_github_event == "ping":
            return Delivery(status="pong")
        try:
            job = issue_job(x_github_event, body)
        except ValidationError as exc:
            raise HTTPException(422, "not an issues event GitHub sends") from exc
        if job is None:
            return Delivery(status="ignored")
        try:
            task_id = await run_in_threadpool(start, job)
        except SdkError as exc:
            raise HTTPException(
                503, "the issue worker is not accepting work; redeliver later"
            ) from exc
        if task_id is None:
            return Delivery(status="already_running")
        return Delivery(status="started", task_id=task_id)

    return api
