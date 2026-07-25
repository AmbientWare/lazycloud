from __future__ import annotations

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService
from execution.functions.service import FunctionControlService
from shared.deployment_records import DeploymentSpec
from shared.deployments import DeploymentKind
from shared.errors import InvalidInputError
from shared.http.functions import FunctionCronRequest


def test_function_cron_rejects_a_stub_from_another_deployment(
    isolated_services: ApiServices,
) -> None:
    first = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="first-cron-function",
            kind=DeploymentKind.CronJob,
            handler="module:first",
        )
    )
    second = isolated_services.deployments.deploy(
        DeploymentSpec(
            name="second-cron-function",
            kind=DeploymentKind.CronJob,
            handler="module:second",
        )
    )
    control = ControlPlaneService(isolated_services.context)
    first_stub = next(stub for stub in control.list_stubs() if stub.deployment_id == first.id)

    with pytest.raises(
        InvalidInputError,
        match="cron schedule must reference its function-like deployment stub",
    ):
        FunctionControlService(isolated_services).function_cron(
            FunctionCronRequest(
                stub_id=first_stub.id,
                deployment_id=second.id,
                cron="every 1m",
            )
        )

    assert isolated_services.cron_jobs.list() == []
