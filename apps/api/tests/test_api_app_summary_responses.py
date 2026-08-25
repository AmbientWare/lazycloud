from api.server.routers.control_plane.apps import _app_summary_response
from database.records.apps import AppRecord
from operations.management import AppOperationalSummary


def test_the_app_summary_carries_every_hourly_series_it_is_given() -> None:
    """The card draws its day out of these, and every one of them has a default.

    A series this mapper forgets is not a failure anywhere. The field is present,
    it is empty, and the hours it described are drawn as unattributed work, which
    is a chart that looks like an app nobody ran rather than a value nobody
    copied. Asserting the series together is what makes adding a fifth one fail
    here rather than in a colour somebody notices weeks later.
    """
    hours = tuple(range(24))
    record = AppOperationalSummary(
        app=AppRecord(id="app-1", workspace_id="workspace-1", name="activity"),
        runs_24h=52,
        failed_runs_24h=2,
        pending_runs_24h=5,
        succeeded_runs_24h=45,
        activity_24h=hours,
        failures_24h=tuple(1 for _ in hours),
        pending_24h=tuple(2 for _ in hours),
        succeeded_24h=tuple(3 for _ in hours),
    )

    response = _app_summary_response(record, can_write=True, scaling={})

    assert response.runs_24h == 52
    assert (
        response.failed_runs_24h,
        response.pending_runs_24h,
        response.succeeded_runs_24h,
    ) == (2, 5, 45)
    assert response.activity_24h == list(hours)
    assert response.failures_24h == [1] * 24
    assert response.pending_24h == [2] * 24
    assert response.succeeded_24h == [3] * 24
