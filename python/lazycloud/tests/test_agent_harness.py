import httpx
import pytest
from lazycloud.agent_harness import AgentHarness, AgentVersionError

import lazycloud
from lazycloud import agent_harness


@pytest.fixture
def registry(monkeypatch):
    """Answers npm's latest lookups from versions and records each request."""
    versions: dict[str, str] = {}
    requested: list[str] = []

    def get(url, **_):
        requested.append(url)
        package = (
            url.removeprefix("https://registry.npmjs.org/")
            .removesuffix("/latest")
            .replace("%2F", "/")
        )
        if package not in versions:
            return httpx.Response(404, request=httpx.Request("GET", url))
        return httpx.Response(
            200, json={"version": versions[package]}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(agent_harness.httpx, "get", get)
    agent_harness.latest_version.cache_clear()
    yield versions, requested
    agent_harness.latest_version.cache_clear()


def test_a_devbox_installs_the_agents_released_at_deploy(registry):
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

    steps = [step.command for step in box.image.definition().steps or [] if step.command]
    assert any("@anthropic-ai/claude-code@9.1.0" in step for step in steps), steps

    versions["@anthropic-ai/claude-code"] = "9.2.0"
    agent_harness.latest_version.cache_clear()
    later = [step.command for step in box.image.definition().steps or [] if step.command]
    assert later != steps, "a newer release changes the build step, so the next build installs it"


def test_an_unreadable_registry_fails_the_deploy_with_its_reason(registry):
    with pytest.raises(AgentVersionError, match="@openai/codex"):
        agent_harness.agent_install_commands([AgentHarness.Codex], lazycloud.Image().architecture)
