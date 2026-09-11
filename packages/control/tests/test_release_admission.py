from pathlib import Path
from uuid import uuid4

from control.release_settings import ReleaseSettings
from control.releases import DeploymentReleaseService
from shared.compute_policy import MachinePool
from shared.releases import ActiveRelease, AgentArtifact, ReleaseTarget
from shared.scheduling import SchedulerWorkerRecord, SchedulerWorkerStatus


def test_activation_preserves_serving_and_fences_upgrade_authority(tmp_path: Path) -> None:
    path = tmp_path / "active.json"
    url = "https://releases.example.com/new/manifest.json"
    service = DeploymentReleaseService(ReleaseSettings(manifest_url=url, active_file=path))
    image = "registry.example.com/worker@sha256:" + "a" * 64
    worker = SchedulerWorkerRecord(
        worker_id="worker",
        pool=MachinePool("default"),
        capacity_owner_id=str(uuid4()),
        runtime_image=image,
        agent_binary_sha256="b" * 64,
    )
    release = ActiveRelease(
        generation=2,
        manifest_url=url,
        target=ReleaseTarget(
            version="2",
            source_revision="c" * 40,
            worker_image=image,
            agent=AgentArtifact(
                url="https://releases.example.com/agent", sha256="b" * 64, size_bytes=1
            ),
        ),
    )
    assert service.admitted_workers([worker]) == []
    path.write_text(release.model_dump_json())
    assert service.admitted_workers([worker]) == [worker]
    assert (
        service.admitted_workers([worker.model_copy(update={"agent_binary_sha256": "d" * 64})])
        == []
    )
    assert service.admitted_workers([worker.model_copy(update={"runtime_image": "older"})]) == []
    previous = worker.model_copy(
        update={
            "status": SchedulerWorkerStatus.Available,
            "runtime_image": "older",
            "admitted_release_generation": 1,
        }
    )
    assert service.worker_registration_generation(previous) == 0
    assert service.worker_registration_generation(worker) == release.generation
    assert service.admitted_workers([previous]) == [previous]
    assert (
        service.admitted_workers(
            [previous.model_copy(update={"status": SchedulerWorkerStatus.Draining})]
        )
        == []
    )
    assert (
        service.admitted_workers(
            [previous.model_copy(update={"status": SchedulerWorkerStatus.Unavailable})]
        )
        == []
    )
    old_url = "https://releases.example.com/old/manifest.json"
    old_replica = DeploymentReleaseService(ReleaseSettings(manifest_url=old_url, active_file=path))
    assert old_replica.admitted_workers([worker]) == [worker]
    assert old_replica.admitted_workers([previous]) == [previous]
    assert not old_replica.controls(release)
    assert service.controls(release)
    rollback = release.model_copy(update={"generation": 3, "manifest_url": old_url})
    path.write_text(rollback.model_dump_json())
    assert service.admitted_workers([worker]) == [worker]
    assert old_replica.admitted_workers([worker]) == [worker]
    assert old_replica.controls(rollback)
    assert not service.controls(rollback)
