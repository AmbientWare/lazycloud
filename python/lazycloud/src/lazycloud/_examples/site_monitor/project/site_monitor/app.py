"""Watch web pages for changes that matter and alert Slack.

Store the secrets with `uv run python -m site_monitor.configure`, then deploy
with `uv run lazycloud deploy site_monitor.app:app`.
"""

import os
from functools import partial

from lazycloud import App, Autoscaler, Image, Map, Secret, current_task_id

from site_monitor.api import create_api
from site_monitor.checks import check_watch
from site_monitor.models import MAX_WATCHES, CheckResult, SweepReport, Watch, load_watches

app = App("site_monitor")

OPENAI_API_KEY = Secret("OPENAI_API_KEY")
SLACK_WEBHOOK_URL = Secret("SLACK_WEBHOOK_URL")

WATCHES = Map("site-monitor-watches")
SNAPSHOTS = Map("site-monitor-snapshots")

image = Image.from_uv(".")
browser_image = (
    Image.from_uv(".", groups=["check"])
    .with_envs({"PLAYWRIGHT_BROWSERS_PATH": "/opt/playwright"})
    .add_commands(["playwright install --with-deps --only-shell chromium"])
)


@app.function(
    name="check-page",
    image=browser_image,
    cpu=2,
    memory="4Gi",
    concurrency=4,
    autoscaler=Autoscaler(max_containers=5, tasks_per_container=4),
    # A retried sweep submits every page again while the first attempt's checks may still queue.
    max_pending_tasks=2 * MAX_WATCHES,
    timeout_seconds=180,
    retries=2,
    retry_delay_seconds=15,
    secrets=[OPENAI_API_KEY.name, SLACK_WEBHOOK_URL.name],
)
def check_page(watch: Watch, run_id: str) -> CheckResult:
    from site_monitor.browser import read_page
    from site_monitor.judge import judge_change
    from site_monitor.slack import post_alert

    return check_watch(
        watch,
        run_id,
        SNAPSHOTS,
        read_page=read_page,
        judge=judge_change,
        notify=partial(post_alert, os.environ[SLACK_WEBHOOK_URL.name]),
    )


@app.function(
    name="sweep",
    image=image,
    cron="*/15 * * * *",
    cpu=0.25,
    memory="256Mi",
    timeout_seconds=900,
    retries=1,
)
def sweep() -> SweepReport:
    # Retries keep the task ID, so a retried sweep skips the checks it finished.
    run_id = current_task_id()
    if not run_id:
        raise RuntimeError("run the sweep remotely: uv run lazycloud run site_monitor.app:sweep")
    watches = load_watches(WATCHES)
    results = list(check_page.map([(watch, run_id) for watch in watches]))
    return SweepReport(
        results=[result for result in results if result is not None],
        failed=[
            str(watch.url) for watch, result in zip(watches, results, strict=True) if result is None
        ],
    )


api = create_api(WATCHES, SNAPSHOTS)
service = app.asgi(
    name="watches",
    image=image,
    cpu=0.25,
    memory="256Mi",
    concurrent_requests=8,
    keep_warm_seconds=60,
)(api)
