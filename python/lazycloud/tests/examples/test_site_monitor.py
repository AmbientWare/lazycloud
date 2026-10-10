from __future__ import annotations

import json
import sys
import threading
import traceback
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from pydantic import ValidationError

import lazycloud

sys.path.insert(0, str(Path(lazycloud.__file__).parent / "_examples" / "site_monitor" / "project"))

from site_monitor.changes import MAX_DIFF_CHARS, normalize_text, text_diff
from site_monitor.checks import check_watch
from site_monitor.models import Outcome, Verdict, Watch, WatchRequest
from site_monitor.slack import SlackError, alert_message, post_alert

PRICING = Watch.from_request(WatchRequest(url="https://example.com/pricing", label="Pricing"))


def test_watches_accept_only_web_pages() -> None:
    for url in ("file:///etc/passwd", "javascript:alert(1)", "ftp://example.com/"):
        with pytest.raises(ValidationError):
            WatchRequest(url=url)
    with pytest.raises(ValidationError):
        WatchRequest(url="https://example.com/", focus="x" * 501)


def test_a_page_and_element_keep_one_watch_id() -> None:
    def watch_id(url: str, selector: str | None = None) -> str:
        return Watch.from_request(WatchRequest(url=url, selector=selector)).id

    assert watch_id("HTTPS://Example.com") == watch_id("https://example.com/")
    assert watch_id("https://example.com/", "#plans") != watch_id("https://example.com/")


def test_layout_whitespace_is_not_a_change() -> None:
    assert normalize_text("  Starter \t $10 \n\n\n Pro  $25 ") == "Starter $10\nPro $25"


def test_the_diff_shows_changed_lines_within_its_budget() -> None:
    assert text_diff("Starter $10\nPro $25", "Starter $12\nPro $25").splitlines() == [
        "@@ -1,2 +1,2 @@",
        "-Starter $10",
        "+Starter $12",
        " Pro $25",
    ]
    long = text_diff("", "\n".join(f"line {n}" for n in range(10_000)))
    assert len(long) <= MAX_DIFF_CHARS + len("\n[diff truncated]")


class Monitor:
    """Drives check_watch the way the deployed function does, with one page and one model."""

    def __init__(self, verdict: Verdict) -> None:
        self.snapshots: dict[str, Any] = {}
        self.page = "Starter $10"
        self.verdict = verdict
        self.reads: list[str] = []
        self.judged: list[str] = []
        self.sent: list[str] = []
        self.slack_down = False

    def check(self, run_id: str) -> Outcome:
        return check_watch(
            PRICING,
            run_id,
            self.snapshots,
            read_page=self.read_page,
            judge=self.judge,
            notify=self.notify,
        ).outcome

    def read_page(self, watch: Watch) -> str:
        self.reads.append(str(watch.url))
        return self.page

    def judge(self, watch: Watch, diff: str) -> Verdict:
        self.judged.append(diff)
        return self.verdict

    def notify(self, watch: Watch, summary: str) -> None:
        if self.slack_down:
            raise SlackError("Slack answered 503")
        self.sent.append(summary)


IMPORTANT = Verdict(important=True, summary="Starter went from $10 to $12.")


def test_first_check_stores_a_baseline_and_unchanged_pages_skip_the_judge() -> None:
    monitor = Monitor(IMPORTANT)
    assert monitor.check("run-1") is Outcome.BASELINE
    assert monitor.check("run-2") is Outcome.UNCHANGED
    assert monitor.judged == []
    assert monitor.sent == []


def test_a_change_that_matters_alerts_once() -> None:
    monitor = Monitor(IMPORTANT)
    monitor.check("run-1")
    monitor.page = "Starter $12"
    assert monitor.check("run-2") is Outcome.ALERTED
    assert monitor.sent == [IMPORTANT.summary]
    assert monitor.check("run-2") is Outcome.ALERTED
    assert monitor.check("run-3") is Outcome.UNCHANGED
    assert monitor.sent == [IMPORTANT.summary]
    assert len(monitor.reads) == 3


def test_a_minor_change_moves_the_baseline_without_alerting() -> None:
    monitor = Monitor(Verdict(important=False, summary="A date changed."))
    monitor.check("run-1")
    monitor.page = "Starter $10 (updated today)"
    assert monitor.check("run-2") is Outcome.MINOR_CHANGE
    assert monitor.check("run-3") is Outcome.UNCHANGED
    assert monitor.sent == []
    assert len(monitor.judged) == 1


def test_a_retry_delivers_an_undelivered_alert_without_judging_again() -> None:
    monitor = Monitor(IMPORTANT)
    monitor.check("run-1")
    monitor.page = "Starter $12"
    monitor.slack_down = True
    with pytest.raises(SlackError):
        monitor.check("run-2")
    monitor.slack_down = False
    monitor.page = "Starter $15"
    assert monitor.check("run-2") is Outcome.ALERTED
    assert monitor.sent == [IMPORTANT.summary]
    assert len(monitor.judged) == 1
    assert len(monitor.reads) == 2


def test_page_text_cannot_mention_the_channel() -> None:
    message = alert_message(PRICING, "Price is <!channel> & <https://evil.example|here>")
    assert "<!channel>" not in message["text"]
    assert "&lt;!channel&gt; &amp; &lt;https://evil.example|here&gt;" in message["text"]
    assert message["text"].endswith("<https://example.com/pricing|Open the page>")


@pytest.fixture
def webhook() -> Iterator[tuple[str, list[dict[str, str]], list[int]]]:
    received: list[dict[str, str]] = []
    status = [200]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["content-length"]))
            received.append(json.loads(body))
            self.send_response(status[0])
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, args=(0.01,))
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/services/T0/B0/hook-secret", received, status
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_slack_receives_the_alert(webhook: tuple[str, list[dict[str, str]], list[int]]) -> None:
    url, received, _ = webhook
    post_alert(url, PRICING, IMPORTANT.summary)
    assert received == [alert_message(PRICING, IMPORTANT.summary)]


def test_slack_errors_never_quote_the_webhook_url(
    webhook: tuple[str, list[dict[str, str]], list[int]],
) -> None:
    url, _, status = webhook
    status[0] = 404
    with pytest.raises(SlackError, match="Slack answered 404") as rejected:
        post_alert(url, PRICING, IMPORTANT.summary)
    closed_port_url = "http://127.0.0.1:1" + urlsplit(url).path
    with pytest.raises(SlackError) as unreachable:
        post_alert(closed_port_url, PRICING, IMPORTANT.summary)
    for error in (rejected.value, unreachable.value):
        assert "hook-secret" not in "".join(traceback.format_exception(error))
