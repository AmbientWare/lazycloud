from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from lazycloud.abstractions.pod import Pod
from lazycloud.agent_harness import AgentHarness, AgentVersionError

import lazycloud
from lazycloud import agent_harness

Registry = tuple[dict[str, str], list[str]]


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[Registry]:
    """Answers npm's latest lookups from versions and records each request."""
    versions: dict[str, str] = {}
    requested: list[str] = []

    def get(url: str, **_: Any) -> httpx.Response:
        requested.append(url)
        package = (
            url.removeprefix("https://registry.npmjs.org/")
            .removesuffix("/latest")
            .replace("%2F", "/")
        )
        request = httpx.Request("GET", url)
        if package not in versions:
            return httpx.Response(404, request=request)
        return httpx.Response(200, json={"version": versions[package]}, request=request)

    monkeypatch.setattr("httpx.get", get)
    agent_harness.latest_version.cache_clear()
    yield versions, requested
    agent_harness.latest_version.cache_clear()


def install_steps(box: Pod) -> list[str]:
    return [step.command for step in box.image.definition().steps or [] if step.command]


def test_a_devbox_installs_the_agents_released_at_deploy(registry: Registry) -> None:
    versions, requested = registry
    versions["@anthropic-ai/claude-code"] = "9.1.0"
    app = lazycloud.App("agents")
    box = app.devbox(
        "box",
        image=lazycloud.Image(),
        disk="10Gi",
        memory="2Gi",
        agent_harnesses=[AgentHarness.ClaudeCode],
    )
    assert requested == [], "defining a devbox reads nothing; containers import the app too"

    steps = install_steps(box)
    assert any("@anthropic-ai/claude-code@9.1.0" in step for step in steps), steps

    versions["@anthropic-ai/claude-code"] = "9.2.0"
    agent_harness.latest_version.cache_clear()
    assert install_steps(box) != steps, (
        "a newer release changes the build step, so the next build installs it"
    )


def test_an_unreadable_registry_fails_the_deploy_with_its_reason(registry: Registry) -> None:
    with pytest.raises(AgentVersionError, match="@openai/codex"):
        agent_harness.agent_install_commands([AgentHarness.Codex], lazycloud.Image().architecture)
