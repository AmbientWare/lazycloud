"""Turn signed GitHub webhook deliveries into issue jobs."""

from __future__ import annotations

import hashlib
import hmac

from pydantic import BaseModel, ConfigDict

from coding_agents.settings import MAX_ISSUE_CHARS, REPOSITORY, TRIGGER_LABEL


class IssueJob(BaseModel):
    model_config = ConfigDict(frozen=True)

    repository: str
    number: int
    title: str
    body: str
    url: str

    @property
    def claim_key(self) -> str:
        return f"issue-{self.number}"


class _Label(BaseModel):
    name: str


class _Issue(BaseModel):
    number: int
    title: str
    body: str | None
    html_url: str


class _Repository(BaseModel):
    full_name: str


class _IssuesEvent(BaseModel):
    action: str
    issue: _Issue
    repository: _Repository
    label: _Label | None = None


def signature_matches(secret: bytes, body: bytes, signature: str | None) -> bool:
    """Whether X-Hub-Signature-256 is the HMAC of the exact body bytes under secret."""
    if signature is None:
        return False
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def issue_job(event: str, body: bytes) -> IssueJob | None:
    """The job a delivery asks for, or None unless it labels an issue of REPOSITORY.

    Only people with triage access can label issues, so the label is the approval
    step between an issue anyone can open and an agent that spends money on it.
    """
    if event != "issues":
        return None
    delivery = _IssuesEvent.model_validate_json(body)
    if (
        delivery.action != "labeled"
        or delivery.label is None
        or delivery.label.name != TRIGGER_LABEL
        or delivery.repository.full_name.lower() != REPOSITORY.lower()
    ):
        return None
    text = delivery.issue.body or ""
    if len(text) > MAX_ISSUE_CHARS:
        text = text[:MAX_ISSUE_CHARS] + "\n\n[The issue continues; it was cut to fit.]"
    return IssueJob(
        repository=delivery.repository.full_name,
        number=delivery.issue.number,
        title=delivery.issue.title,
        body=text,
        url=delivery.issue.html_url,
    )
