"""Render a page in headless Chromium and return its readable text."""

from contextlib import suppress

import trafilatura
from playwright.sync_api import Route, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from site_monitor.changes import normalize_text
from site_monitor.models import Watch

LOAD_TIMEOUT_MS = 30_000
SETTLE_TIMEOUT_MS = 5_000
SKIPPED_RESOURCES = frozenset({"image", "media", "font"})


class PageError(RuntimeError):
    pass


def read_page(watch: Watch) -> str:
    # A container's /dev/shm is too small for Chromium's shared memory.
    with (
        sync_playwright() as playwright,
        playwright.chromium.launch(args=["--disable-dev-shm-usage"]) as browser,
    ):
        page = browser.new_page()
        page.route("**/*", _skip_heavy_resources)
        response = page.goto(str(watch.url), wait_until="load", timeout=LOAD_TIMEOUT_MS)
        if response is not None and not response.ok:
            raise PageError(f"{watch.url} answered {response.status}")
        # Pages that poll forever never go idle; read what has rendered by then.
        with suppress(PlaywrightTimeoutError):
            page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
        if watch.selector is not None:
            raw = page.locator(watch.selector).first.inner_text(timeout=LOAD_TIMEOUT_MS)
        else:
            raw = trafilatura.extract(page.content(), favor_recall=True) or ""
    return normalize_text(raw)


def _skip_heavy_resources(route: Route) -> None:
    if route.request.resource_type in SKIPPED_RESOURCES:
        route.abort()
    else:
        route.continue_()
