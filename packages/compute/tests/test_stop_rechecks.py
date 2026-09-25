from __future__ import annotations

import time

from compute.stop_rechecks import StopRechecks


def test_a_unit_is_rechecked_until_its_stop_is_recorded_then_left_alone() -> None:
    answers = [True, True, False]
    calls: list[tuple[str, str]] = []

    def recheck(workspace_id: str, capacity_owner_id: str) -> bool:
        calls.append((workspace_id, capacity_owner_id))
        return answers.pop(0) if answers else False

    rechecks = StopRechecks(recheck, interval_seconds=0.01)
    rechecks.watch("workspace", "unit")
    rechecks.watch("workspace", "unit")

    deadline = time.monotonic() + 5
    while len(calls) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.1)

    assert calls == [("workspace", "unit")] * 3
