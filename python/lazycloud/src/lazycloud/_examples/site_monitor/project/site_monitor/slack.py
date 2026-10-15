"""Post change alerts to a Slack incoming webhook."""

import httpx

from site_monitor.models import Watch


class SlackError(RuntimeError):
    pass


def alert_message(watch: Watch, summary: str) -> dict[str, str]:
    title = _escape(watch.label or str(watch.url))
    return {"text": f"*{title}* changed: {_escape(summary)}\n<{watch.url}|Open the page>"}


def post_alert(webhook_url: str, watch: Watch, summary: str) -> None:
    try:
        response = httpx.post(webhook_url, json=alert_message(watch, summary), timeout=10)
    except httpx.HTTPError as exc:
        # The webhook URL is the credential, and httpx errors quote it.
        raise SlackError(f"could not reach Slack: {type(exc).__name__}") from None
    if response.is_error:
        raise SlackError(f"Slack answered {response.status_code}")


def _escape(text: str) -> str:
    # Page text reaches the summary, so it must not become a mention or a link.
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
