"""Check one watched page: read it, compare with the last snapshot, judge, alert."""

from collections.abc import Callable, MutableMapping
from datetime import UTC, datetime
from typing import Any

from site_monitor.changes import text_diff
from site_monitor.models import CheckResult, Outcome, Snapshot, Verdict, Watch

ReadPage = Callable[[Watch], str]
Judge = Callable[[Watch, str], Verdict]
Notify = Callable[[Watch, str], None]


def check_watch(
    watch: Watch,
    run_id: str,
    snapshots: MutableMapping[str, Any],
    *,
    read_page: ReadPage,
    judge: Judge,
    notify: Notify,
) -> CheckResult:
    """Run one check, at most once per run.

    The snapshot is saved before the alert goes out and again once it has, so
    a retry of the same run resends an undelivered alert without reading or
    judging the page again, and never resends a delivered one.
    """
    stored = snapshots.get(watch.id)
    previous = Snapshot.model_validate(stored) if stored is not None else None
    if previous is not None and previous.run_id == run_id:
        snapshot = previous
    else:
        snapshot = _observe(watch, run_id, previous, read_page, judge)
        snapshots[watch.id] = snapshot.model_dump(mode="json")
    if snapshot.pending_alert is not None:
        notify(watch, snapshot.pending_alert)
        snapshot = snapshot.model_copy(update={"pending_alert": None})
        snapshots[watch.id] = snapshot.model_dump(mode="json")
    return CheckResult(watch_id=watch.id, url=str(watch.url), outcome=snapshot.outcome)


def _observe(
    watch: Watch,
    run_id: str,
    previous: Snapshot | None,
    read_page: ReadPage,
    judge: Judge,
) -> Snapshot:
    text = read_page(watch)
    alert = None
    if previous is None:
        outcome = Outcome.BASELINE
    elif text == previous.text:
        outcome = Outcome.UNCHANGED
    else:
        verdict = judge(watch, text_diff(previous.text, text))
        print(f"{watch.url} changed: {verdict.summary}", flush=True)
        outcome = Outcome.ALERTED if verdict.important else Outcome.MINOR_CHANGE
        alert = verdict.summary if verdict.important else None
    return Snapshot(
        run_id=run_id,
        checked_at=datetime.now(UTC),
        text=text,
        outcome=outcome,
        pending_alert=alert,
    )
