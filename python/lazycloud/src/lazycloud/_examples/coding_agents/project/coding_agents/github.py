"""GitHub's REST API for comments and pull requests, and git for pushing a branch."""

from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path
from types import TracebackType

import httpx
from pydantic import BaseModel, TypeAdapter

from coding_agents.settings import COMMIT_AUTHOR, COMMIT_EMAIL


class GitHubError(RuntimeError):
    pass


class GitCommandError(RuntimeError):
    pass


class PullRequest(BaseModel):
    number: int
    html_url: str


_PULL_REQUESTS = TypeAdapter(list[PullRequest])


class GitHub:
    """One repository's issues and pull requests, with a token that may write to them."""

    def __init__(self, repository: str, token: str) -> None:
        self.repository = repository
        self._http = httpx.Client(
            base_url=f"https://api.github.com/repos/{repository}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
        )

    def __enter__(self) -> GitHub:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._http.close()

    def comment(self, issue: int, body: str) -> None:
        self._send("POST", f"/issues/{issue}/comments", json={"body": body})

    def open_pull_request(self, *, branch: str, base: str, title: str, body: str) -> PullRequest:
        """Open a pull request from branch, or update the one already open from it."""
        owner = self.repository.split("/")[0]
        found = self._send("GET", "/pulls", params={"head": f"{owner}:{branch}", "state": "open"})
        existing = _PULL_REQUESTS.validate_json(found.content)
        content = {"title": title, "body": body}
        if existing:
            updated = self._send("PATCH", f"/pulls/{existing[0].number}", json=content)
            return PullRequest.model_validate_json(updated.content)
        created = self._send("POST", "/pulls", json={**content, "head": branch, "base": base})
        return PullRequest.model_validate_json(created.content)

    def _send(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        response = self._http.request(method, path, json=json, params=params)
        if response.is_error:
            raise GitHubError(f"GitHub answered {response.status_code}: {response.text[:500]}")
        return response


def git_auth_env(token: str) -> dict[str, str]:
    """Git settings that send token to github.com without writing it to any file or argument."""
    credentials = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credentials}",
    }


def push_patch(
    *,
    remote: str,
    base_commit: str,
    branch: str,
    patch: Path,
    message: str,
    workdir: Path,
    env: dict[str, str],
) -> str:
    """Commit patch on top of base_commit, force-push it to branch and return the commit.

    A shallow fetch of the one base commit is all the history a patch needs. The
    push replaces the branch, so a repeated run for an issue updates its pull
    request instead of opening another.
    """
    git_env = {**os.environ, **env, "GIT_TERMINAL_PROMPT": "0"}

    def git(*args: str) -> str:
        done = subprocess.run(
            ["git", *args],
            cwd=workdir,
            env=git_env,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        if done.returncode != 0:
            raise GitCommandError(f"git {args[0]} failed: {done.stderr.strip()[-1000:]}")
        return done.stdout.strip()

    git("init", "--quiet")
    git("fetch", "--quiet", "--depth", "1", remote, base_commit)
    git("checkout", "--quiet", "--detach", "FETCH_HEAD")
    git("apply", "--index", "--binary", str(patch))
    git(
        "-c",
        f"user.name={COMMIT_AUTHOR}",
        "-c",
        f"user.email={COMMIT_EMAIL}",
        "commit",
        "--quiet",
        "-m",
        message,
    )
    git("push", "--quiet", "--force", remote, f"HEAD:refs/heads/{branch}")
    return git("rev-parse", "HEAD")
