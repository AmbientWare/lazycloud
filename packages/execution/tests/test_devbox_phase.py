from __future__ import annotations

import pytest
from database.repositories.orchestration import LiveContainer
from execution.pods.devboxes import devbox_phase
from shared.containers import ContainerExecutionPhase, ContainerStatus
from shared.deployments import DevboxPhase

_IMAGE = ContainerExecutionPhase.LoadImage
_ROOTFS = ContainerExecutionPhase.PrepareRootfs
_NONE: frozenset[ContainerExecutionPhase] = frozenset()


def _container(status: ContainerStatus, *, placed: bool = True) -> LiveContainer:
    return LiveContainer(id="c", status=status, started_at=None, placed=placed)


@pytest.mark.parametrize(
    ("container", "steps", "saving", "failure", "expected"),
    [
        (_container(ContainerStatus.Pending, placed=False), _NONE, False, None, "queued"),
        (_container(ContainerStatus.Pending), _NONE, False, None, "pulling_image"),
        (_container(ContainerStatus.Pending), frozenset({_IMAGE}), False, None, "restoring_disk"),
        (
            _container(ContainerStatus.Pending),
            frozenset({_IMAGE, _ROOTFS}),
            False,
            None,
            "starting",
        ),
        (_container(ContainerStatus.Running), _NONE, False, "old", "running"),
        (None, _NONE, True, "old", "stopping"),
        (None, _NONE, False, "image not found", "failed"),
        (None, _NONE, False, None, "stopped"),
    ],
)
def test_a_devbox_phase_follows_its_container_records(
    container: LiveContainer | None,
    steps: frozenset[ContainerExecutionPhase],
    saving: bool,
    failure: str | None,
    expected: str,
) -> None:
    phase, reason = devbox_phase(
        container,
        finished_steps=steps,
        saving_disk=saving,
        waking=False,
        refusal=None,
        recent_failure=failure,
    )

    assert phase is DevboxPhase(expected)
    assert reason == (failure if phase is DevboxPhase.Failed else "")


@pytest.mark.parametrize(
    ("refusal", "expected"), [(None, DevboxPhase.Queued), ("unfunded", DevboxPhase.Failed)]
)
def test_a_start_waits_for_a_container_unless_the_autoscaler_refused_one(
    refusal: str | None, expected: DevboxPhase
) -> None:
    phase, reason = devbox_phase(
        None,
        finished_steps=_NONE,
        saving_disk=False,
        waking=True,
        refusal=refusal,
        recent_failure="an older failure",
    )

    assert (phase, reason) == (expected, refusal or "")
