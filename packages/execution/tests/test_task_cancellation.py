from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from api.server.services import ApiServices
from shared.function_payloads import FunctionJsonResult
from shared.tasks import TaskStatus


def test_competing_cancellations_settle_a_retry_once(isolated_services: ApiServices) -> None:
    tasks = isolated_services.tasks
    task = tasks.create("retrying")
    tasks.transition(task, TaskStatus.Retry, error="retry due")
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(tasks.cancel, [task.id, task.id]))
    assert sum(outcome.state_changed for outcome in outcomes) == 1
    assert all(outcome.task.status is TaskStatus.Cancelled for outcome in outcomes)

    completed = tasks.create("completed")
    result = FunctionJsonResult(value=7)
    tasks.transition(completed, TaskStatus.Complete, function_result=result, exit_code=0)
    cancellation = tasks.cancel(completed.id)
    assert not cancellation.state_changed
    assert cancellation.task.status is TaskStatus.Complete
    assert cancellation.task.function_result == result
