"""The app, the image its functions share, and the state they keep."""

from __future__ import annotations

from lazycloud import App, Image, Map

APP_NAME = "coding_agents"

app = App(APP_NAME)

# The webhook, the issue worker and the eval all run this project's locked
# dependencies; the worker also pushes branches with git.
function_image = Image.from_uv(".").add_commands(
    [
        "apt-get update && apt-get install -y --no-install-recommends git "
        "&& rm -rf /var/lib/apt/lists/*"
    ]
)

# Holds the repository snapshot record and one claim per issue being worked on.
state = Map("coding-agents")
