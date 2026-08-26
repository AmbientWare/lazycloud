from __future__ import annotations

from typing import Any

import pytest
from shared.http.errors import ErrorResponse, HttpApiError

from cli import fleet


class _Client:
    """A unit delete that answers the way the real route does."""

    def __init__(self, answers: list[HttpApiError | None]) -> None:
        self.answers = answers
        self.calls = 0

    def delete_unit(self, unit_id: str) -> None:
        del unit_id
        self.calls += 1
        answer = self.answers.pop(0)
        if answer is not None:
            raise answer


def _api_error(status_code: int, code: str) -> HttpApiError:
    return HttpApiError(
        code,
        status_code=status_code,
        error=ErrorResponse(detail=code, code=code),
    )


def _use_client(monkeypatch: pytest.MonkeyPatch, client: _Client) -> None:
    def _client(workspace: str | None = None) -> _Client:
        del workspace
        return client

    monkeypatch.setattr(fleet, "admin_api_client", _client)


def _destroy(
    monkeypatch: pytest.MonkeyPatch,
    client: _Client,
    **kwargs: Any,
) -> fleet.FleetUnitTeardown:
    _use_client(monkeypatch, client)

    def _no_wait(seconds: float) -> None:
        del seconds

    monkeypatch.setattr(fleet.time, "sleep", _no_wait)
    return fleet._destroy_unit(
        workspace_id="ws",
        unit_id="unit-1",
        unit_name="managed-pool",
        timeout_seconds=kwargs.get("timeout_seconds", 600.0),
        interval_seconds=kwargs.get("interval_seconds", 0.01),
    )


def test_a_held_capacity_lease_is_waited_out_rather_than_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole reason this command exists.

    The lease a delete contends with is held for up to five minutes and clears
    on its own. Reading it as a refusal is what left two units standing, and the
    scheduler rebuilt their autoscaling groups from the rows that survived.
    """

    client = _Client(
        [
            _api_error(409, "capacity_reservation_lock_contended"),
            _api_error(409, "capacity_reservation_lock_contended"),
            None,
        ]
    )

    outcome = _destroy(monkeypatch, client)

    assert outcome.deleted
    assert outcome.attempts == 3
    assert client.calls == 3


def test_a_provider_still_tearing_down_is_waited_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting an autoscaling group returns before the group is gone.

    The first delete tears it down and reports the provider unavailable; a later
    one removes the launch template and succeeds. Both are the same teardown, so
    only the second answer means anything.
    """

    client = _Client([_api_error(503, "upstream_unavailable"), None])

    outcome = _destroy(monkeypatch, client)

    assert outcome.deleted
    assert outcome.attempts == 2


def test_a_unit_holding_reservations_is_reported_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not every conflict is a lease.

    A unit with open reservations answers 409 too, and waiting does not change
    it. Retrying that one until the budget runs out would turn a fact into a
    timeout and bury the reason.
    """

    client = _Client([_api_error(409, "conflict")])

    outcome = _destroy(monkeypatch, client)

    assert not outcome.deleted
    assert outcome.attempts == 1
    assert outcome.reason == "conflict"


def test_a_unit_already_gone_counts_as_done(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absent is the state this is trying to reach, however it got there."""

    outcome = _destroy(monkeypatch, _Client([_api_error(404, "not_found")]))

    assert outcome.deleted
    assert outcome.reason == "already absent"


def test_a_lease_that_never_clears_ends_the_budget_and_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A teardown that cannot finish has to say so.

    Reporting success here is the one outcome worth preventing: the next step
    destroys the cluster that would otherwise have stopped the machines.
    """

    client = _Client([_api_error(409, "capacity_reservation_lock_contended")] * 200)
    # Real waiting here, briefly. The budget is a wall-clock deadline, so a
    # stubbed sleep would spin without ever reaching it and prove nothing about
    # the thing under test.
    _use_client(monkeypatch, client)

    outcome = fleet._destroy_unit(
        workspace_id="ws",
        unit_id="unit-1",
        unit_name="managed-pool",
        timeout_seconds=0.12,
        interval_seconds=0.04,
    )

    assert not outcome.deleted
    assert outcome.reason == "capacity_reservation_lock_contended"
    assert 1 < client.calls < 10
