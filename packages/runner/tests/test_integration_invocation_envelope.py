from __future__ import annotations

import pytest
from runner.function import decode_function_invocation
from runner.invocation import cloudpickle_bytes
from shared.function_payloads import FunctionCloudpickleInvocation
from shared.http.functions import FunctionClaimedTask


def test_function_runner_rejects_untyped_invocation_envelopes() -> None:
    with pytest.raises(ValueError, match="invalid function invocation envelope"):
        decode_function_invocation(
            FunctionClaimedTask(
                task_id="task-1",
                invocation=FunctionCloudpickleInvocation.from_bytes(
                    cloudpickle_bytes(["not", "an", "envelope"])
                ),
            )
        )
