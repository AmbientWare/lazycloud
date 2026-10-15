"""Store the three credentials the system reads as workspace secrets.

    uv run python -m coding_agents.configure

Input stays hidden. Leave a prompt empty to keep the value already stored.
"""

from getpass import getpass

from coding_agents.app import ANTHROPIC_API_KEY, GITHUB_TOKEN, GITHUB_WEBHOOK_SECRET

if __name__ == "__main__":
    for secret, label in (
        (ANTHROPIC_API_KEY, "Anthropic API key"),
        (
            GITHUB_TOKEN,
            "GitHub token with read and write access to contents, issues and pull requests",
        ),
        (GITHUB_WEBHOOK_SECRET, "GitHub webhook secret"),
    ):
        value = getpass(f"{label}: ")
        if value:
            secret.set(value)
