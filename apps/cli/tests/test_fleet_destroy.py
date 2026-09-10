from __future__ import annotations

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
    *,
    timeout_seconds: float = 600.0,
    interval_seconds: float = 0.01,
) -> fleet.FleetUnitTeardown:
    _use_client(monkeypatch, client)

    def _no_wait(seconds: float) -> None:
        del seconds

    monkeypatch.setattr(fleet.time, "sleep", _no_wait)
    return fleet._destroy_unit(
        workspace_id="ws",
        unit_id="unit-1",
        unit_name="managed-pool",
        timeout_seconds=timeout_seconds,
        interval_seconds=interval_seconds,
    )


def test_a_held_capacity_lease_is_waited_out_rather_than_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

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

    client = _Client([_api_error(503, "upstream_unavailable"), None])

    outcome = _destroy(monkeypatch, client)

    assert outcome.deleted
    assert outcome.attempts == 2


def test_a_unit_holding_reservations_is_reported_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    client = _Client([_api_error(409, "conflict")])

    outcome = _destroy(monkeypatch, client)

    assert not outcome.deleted
    assert outcome.attempts == 1
    assert outcome.reason == "conflict"


def test_a_unit_already_gone_counts_as_done(monkeypatch: pytest.MonkeyPatch) -> None:

    outcome = _destroy(monkeypatch, _Client([_api_error(404, "not_found")]))

    assert outcome.deleted
    assert outcome.reason == "already absent"


def test_a_lease_that_never_clears_ends_the_budget_and_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    client = _Client([_api_error(409, "capacity_reservation_lock_contended")] * 200)
    _use_client(monkeypatch, client)
    elapsed = 0.0

    def advance(seconds: float) -> None:
        nonlocal elapsed
        elapsed += seconds

    monkeypatch.setattr(fleet.time, "monotonic", lambda: elapsed)
    monkeypatch.setattr(fleet.time, "sleep", advance)

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
