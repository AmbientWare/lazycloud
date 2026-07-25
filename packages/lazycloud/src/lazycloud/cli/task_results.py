from __future__ import annotations

from shared.deployments import StubKind
from shared.function_payloads import FunctionPayloadEncoding
from shared.http.tasks import TaskResponse

from lazycloud.function_results import FunctionResultDecodeError, parse_function_result

_FUNCTION_RESULT_KINDS = frozenset({StubKind.Function, StubKind.CronJob})


def task_result_human_value(task: TaskResponse) -> object:
    workload = task.workload
    if task.result is None or workload is None or workload.kind not in _FUNCTION_RESULT_KINDS:
        return task.result
    try:
        payload = parse_function_result(task.result)
    except FunctionResultDecodeError:
        return task.result
    if payload.encoding is FunctionPayloadEncoding.Json:
        return payload.value
    return {
        "encoding": FunctionPayloadEncoding.Cloudpickle.value,
        "size_bytes": len(payload.bytes_value()),
        "message": (
            "Opaque Python result; decode only through a trusted Function invocation handle."
        ),
    }


__all__ = ["task_result_human_value"]
