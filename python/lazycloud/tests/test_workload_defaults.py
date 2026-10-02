from __future__ import annotations

import pytest
from pydantic import ValidationError
from shared.deployment_records import (
    DeploymentSpec,
)
from shared.deployments import DeploymentKind, PodRole
from shared.disks import DiskMount

_ROOT = DiskMount(name="home", size_bytes=1024**3)


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        (
            {"role": PodRole.Devbox, "root_disk_bytes": 1024**3, "metadata": {"ssh": False}},
            "cannot turn ssh off",
        ),
        (
            {
                "role": PodRole.Devbox,
                "root_disk_bytes": 1024**3,
                "metadata": {"autoscaler": {"max_containers": 2}},
            },
            "runs one container",
        ),
        ({"role": PodRole.Devbox}, "needs a root disk"),
        (
            {"role": PodRole.Devbox, "root_disk_bytes": 1024**3, "disks": [_ROOT]},
            "not both",
        ),
        ({"root_disk_bytes": 1024**3}, "only supported for devboxes"),
        ({"kind": DeploymentKind.Function, "role": PodRole.Devbox}, "role is only supported"),
    ],
)
def test_a_devbox_refuses_settings_that_contradict_it(
    spec: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        DeploymentSpec.model_validate({"name": "box", "kind": DeploymentKind.Pod, **spec})
