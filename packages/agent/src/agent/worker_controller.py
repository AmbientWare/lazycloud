from __future__ import annotations

import hashlib
import logging
import math
import os
import random
import socket
import subprocess
import time
from collections.abc import Callable, Collection, Sequence
from concurrent.futures import CancelledError, Future
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock, Timer
from typing import Protocol

from pydantic import JsonValue, TypeAdapter
from shared.app_identity import AGENT_NAME, NAME, WORKER_ADMISSION_WAITING_FILE
from shared.compute_enrollment import AgentWorkerSlotStatus
from shared.contracts import ContractModel
from shared.step_timings import StepTimings, seconds_since_boot
from worker.configuration import WorkerConfiguration, serialize_worker_configuration

from agent.image_preparation import WorkerImagePreparation
from agent.operations import (
    AGENT_MANAGED_LABEL,
    AGENT_WORKER_ID_LABEL,
    WORKER_ADMISSION_HOLD_ENV,
    AgentBootstrap,
    AgentWorkerContainerPlan,
    AgentWorkerNetwork,
    AgentWorkerReconcileAction,
    AgentWorkerReconcilePlan,
    AgentWorkerSlot,
    WorkerSlotAction,
    build_agent_worker_dirs,
    plan_worker_container,
    sanitize_worker_name,
)
from agent.state import model_payload, write_json_atomic
from agent.updates import AgentUpdater

LOGGER = logging.getLogger(__name__)
WORKER_IMAGE_CHECK_WAIT_SECONDS = 3.0
AGENT_ACTIVE_SLOTS_FILE = "active-worker-slots.json"
AGENT_LAST_PREPARED_IMAGE_FILE = "last-prepared-worker-image.json"
AGENT_RESERVE_WORKER_FILE = "reserve-worker.json"
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
BOOT_IMAGE_LOOKUP_SECONDS = 3.0
DOCKER_WAIT_SECONDS = 60.0
DOCKER_PING_SECONDS = 0.5
DOCKER_POLL_SECONDS = 0.1
DEFAULT_DOCKER_SOCKET = "/var/run/docker.sock"
BOOT_WORKER_START_SECONDS = 60.0
WORKER_EXIT_LOG_LINES = 200
_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class WorkerImagePullError(RuntimeError):
    """The exact worker image could not be made available on this host."""


class AgentReserveWorker(ContractModel):
    """The credential-bearing slot retained across a reserve's stop or hibernation."""

    boot_id: str
    agent_binary_sha256: str
    slot: AgentWorkerSlot


class CommandRunner(Protocol):
    def run(self, args: list[str], *, stop: Event | None = None) -> CommandResult: ...


class CommandResult(ContractModel):
    args: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(slots=True)
class SubprocessCommandRunner:
    def run(self, args: list[str], *, stop: Event | None = None) -> CommandResult:
        deadline = time.monotonic() + 300
        with subprocess.Popen(
            args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ) as process:
            try:
                while True:
                    if stop is not None and stop.is_set():
                        raise CancelledError("worker image preparation stopped")
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(args, 300)
                    try:
                        stdout, stderr = process.communicate(timeout=0.2)
                        return CommandResult(
                            args=args,
                            returncode=process.returncode,
                            stdout=stdout,
                            stderr=stderr,
                        )
                    except subprocess.TimeoutExpired:
                        continue
            except BaseException:
                process.kill()
                process.communicate()
                raise


def docker_socket_path() -> str | None:
    """Resolve the Docker CLI endpoint; return None for remote or unreadable contexts."""
    host = os.environ.get("DOCKER_HOST", "")
    if not host:
        config_dir = Path(os.environ.get("DOCKER_CONFIG") or Path.home() / ".docker")
        context = os.environ.get("DOCKER_CONTEXT", "")
        if not context:
            try:
                config = _JSON_VALUE_ADAPTER.validate_json(
                    (config_dir / "config.json").read_bytes()
                )
            except FileNotFoundError:
                config = None
            except (OSError, ValueError):
                return None
            current = config.get("currentContext") if isinstance(config, dict) else None
            context = current if isinstance(current, str) else ""
        if context and context != "default":
            digest = hashlib.sha256(context.encode()).hexdigest()
            try:
                meta = _JSON_VALUE_ADAPTER.validate_json(
                    (config_dir / "contexts" / "meta" / digest / "meta.json").read_bytes()
                )
            except (OSError, ValueError):
                return None
            endpoints = meta.get("Endpoints") if isinstance(meta, dict) else None
            docker = endpoints.get("docker") if isinstance(endpoints, dict) else None
            named = docker.get("Host") if isinstance(docker, dict) else None
            if not isinstance(named, str):
                return None
            host = named
    if not host:
        return DEFAULT_DOCKER_SOCKET
    return host.removeprefix("unix://") if host.startswith("unix://") else None


def _docker_ping_failure(path: str) -> str:
    """Return a local Docker API ping failure, or an empty string on success."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(DOCKER_PING_SECONDS)
            connection.connect(path)
            connection.sendall(b"GET /_ping HTTP/1.0\r\nHost: docker\r\n\r\n")
            reply = connection.recv(64)
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"
    status = reply.split(b"\r\n", 1)[0]
    if status.startswith(b"HTTP/1.") and status.split(b" ")[1:2] == [b"200"]:
        return ""
    return status.decode("ascii", "replace") or "empty reply"


def _slot_removal_is_settled(detail: str) -> bool:
    """An absent container or removal already in progress needs no further stop."""
    message = detail.casefold()
    return "already in progress" in message or "no such container" in message


@dataclass(frozen=True, slots=True)
class WorkerObservation:
    slot: AgentWorkerSlot
    admission_held: bool


@dataclass(slots=True)
class DockerAgentWorkerController:
    state_dir: Path
    docker_binary: str = "docker"
    worker_image_override: str = ""
    worker_network: AgentWorkerNetwork = field(default_factory=AgentWorkerNetwork)
    runner: CommandRunner = field(default_factory=SubprocessCommandRunner)
    host_aliases: list[str] = field(default_factory=list)
    platform: str = ""
    report_worker_exit: Callable[[str, str], None] | None = None
    disk_volume_slots: Callable[[], int] | None = None
    """How many disk volumes this provider machine can still attach; unset on a
    joined machine, whose worker keeps disks on host storage."""

    _images: WorkerImagePreparation = field(init=False)
    _last_prepared: str | None = field(default=None, init=False)
    _reserve_worker: AgentReserveWorker | None = field(default=None, init=False)
    _reserve_worker_on_disk: bool = field(default=True, init=False)
    """False once this process removed the record and has not written one since."""
    _docker_answered: bool = field(default=False, init=False)
    _docker_waited_out: bool = field(default=False, init=False)
    """The minute passed without an answer, so later calls run Docker directly."""
    docker_wait_seconds: float = 0.0
    """How long after the controller starts its Docker calls wait for the daemon.

    Only the daemon sets it, because its unit starts before Docker. A command
    such as uninstall, and every stop, runs Docker at once and fails fast.
    """
    _docker_deadline: float = field(default=0.0, init=False)
    _docker_socket: str | None = field(default=None, init=False)
    _closing: Event = field(default_factory=Event, init=False)
    _halted: Event = field(default_factory=Event, init=False)
    """Set once the capacity shutdown removes every worker; no worker starts after."""
    _slots_lock: Lock = field(default_factory=Lock, init=False)
    """Held only to read or write the slot file. The capacity shutdown runs beside
    the stream, and this orders a start's record against its halt."""

    def __post_init__(self) -> None:
        self._images = WorkerImagePreparation(self._prepare_worker_image)
        self._docker_deadline = time.monotonic() + self.docker_wait_seconds
        # Resolved once: another container CLI, or a socket that is not local
        # or cannot be resolved, means no wait, and Docker calls run at once.
        self._docker_socket = (
            docker_socket_path()
            if self.docker_wait_seconds and Path(self.docker_binary).name == "docker"
            else None
        )

    def wake_on_image_prepared(self, wake: Callable[[], None] | None) -> None:
        """Call `wake` whenever a worker image finishes preparing, however it ended."""
        self._images.on_finished = wake

    def wait_for_docker(self, stop: Event | None = None) -> bool:
        """Bound Docker startup by the deadline set when this controller was created."""
        if self._docker_answered:
            return True
        socket_path = self._docker_socket
        if self._docker_waited_out or socket_path is None:
            return False
        began = time.monotonic()
        while failure := _docker_ping_failure(socket_path):
            if time.monotonic() >= self._docker_deadline:
                LOGGER.warning("docker did not answer after agent start: %s", failure)
                self._docker_waited_out = True
                return False
            if self._closing.wait(DOCKER_POLL_SECONDS) or (stop is not None and stop.is_set()):
                return False
        LOGGER.info(
            "docker answered after %.2fs, at boot+%.2fs",
            time.monotonic() - began,
            seconds_since_boot(),
        )
        self._docker_answered = True
        return True

    def _docker(self, args: list[str], *, stop: Event | None = None) -> CommandResult:
        """Run a command that needs the Docker daemon, once it has answered or had its time."""
        self.wait_for_docker(stop)
        return self.runner.run(args, stop=stop)

    def _docker_failure(self, args: list[str], *, within: float) -> str:
        """Why a docker command failed within `within` seconds, or empty when it succeeded."""
        expired = Event()
        timer = Timer(within, expired.set)
        timer.start()
        try:
            result = self.runner.run(args, stop=expired)
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"
        finally:
            timer.cancel()
        if result.returncode == 0:
            return ""
        return (result.stderr or result.stdout).strip()[-300:] or f"exit {result.returncode}"

    def close(self) -> None:
        self._closing.set()
        self._images.close()

    def prepare_worker_image(self) -> Future[None] | None:
        """Prepare the override image, or check the saved image without pulling it."""
        if self.worker_image_override:
            return self._images.start(self.worker_image_override)
        image = self._last_prepared_image()
        if image:
            self._images.look_up(image, self._image_present)
        return None

    def _image_present(self, image: str, stop: Event | None = None) -> bool:
        """Bound the local image lookup; a failed lookup leaves preparation to the stream."""
        if not self.wait_for_docker(stop) and (
            self._closing.is_set() or (stop is not None and stop.is_set())
        ):
            return False
        failure = self._docker_failure(
            [self.docker_binary, "image", "inspect", image], within=BOOT_IMAGE_LOOKUP_SECONDS
        )
        if failure:
            LOGGER.warning("did not find %s on the host: %s", image, failure)
        return not failure

    def prepared_worker_images(self) -> list[str]:
        prepared = self._images.prepared()
        latest = self._images.latest
        if latest and latest != self._last_prepared_image():
            write_json_atomic(self.last_prepared_image_path, latest, permissions=0o600)
            self._last_prepared = latest
        return prepared

    @property
    def last_prepared_image_path(self) -> Path:
        return self.state_dir / AGENT_LAST_PREPARED_IMAGE_FILE

    def _last_prepared_image(self) -> str:
        """A damaged image hint can be discarded because the control plane supplies the image."""
        if self._last_prepared is not None:
            return self._last_prepared
        path = self.last_prepared_image_path
        recorded: object = ""
        try:
            recorded = _JSON_VALUE_ADAPTER.validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            LOGGER.warning("ignoring the unreadable worker image record %s", path, exc_info=True)
            with suppress(OSError):
                path.unlink(missing_ok=True)
        self._last_prepared = recorded if isinstance(recorded, str) else ""
        return self._last_prepared

    def has_unreported_image(self, reported: Collection[str]) -> bool:
        """Whether the next stream has an image outcome the last one did not report."""
        return self._images.unreported(reported)

    def pull_detached(self, images: Collection[str]) -> None:
        """Keep image pulls alive across the agent's process replacement during an update."""
        # A failed pull in progress is the updated agent's to report; it must not
        # stop this update, so the pending result is left uncollected here.
        for image in sorted(set(images) - self._images.known()):
            try:
                self._docker(
                    [
                        "sh",
                        "-c",
                        '"$0" pull --quiet "$1" >/dev/null 2>&1 &',
                        self.docker_binary,
                        image,
                    ]
                )
            except (OSError, subprocess.SubprocessError):
                LOGGER.warning("could not start pulling %s before the update", image, exc_info=True)
                continue
            LOGGER.info("pulling %s while the agent updates", image)

    def wait_for_boot_image(self, timeout_seconds: float) -> None:
        """Wait for the image started at boot, prepared or looked up, to finish."""
        self._images.wait_for_started(timeout_seconds)

    def _prepare_worker_image(self, image: str, stop: Event) -> None:
        try:
            self._pull_worker_image(image, stop)
        except subprocess.TimeoutExpired as exc:
            raise WorkerImagePullError(f"worker image preparation timed out: {image}") from exc

    def _pull_worker_image(self, image: str, stop: Event) -> None:
        inspected = self._docker([self.docker_binary, "image", "inspect", image], stop=stop)
        if inspected.returncode == 0:
            return
        failures: list[str] = []
        for attempt in range(3):
            pulled = self._docker([self.docker_binary, "pull", image], stop=stop)
            if pulled.returncode == 0:
                return
            failures.append((pulled.stderr or pulled.stdout).strip()[-1000:])
            if attempt < 2 and stop.wait(2**attempt + random.uniform(0, 0.5)):
                raise CancelledError("worker image preparation stopped")
        detail = failures[-1] if failures else "docker returned no diagnostic"
        raise WorkerImagePullError(f"pull worker image {image} failed: {detail}")

    @property
    def reserve_worker_path(self) -> Path:
        return self.state_dir / AGENT_RESERVE_WORKER_FILE

    def record_reserve_worker(self, slot: AgentWorkerSlot) -> None:
        record = AgentReserveWorker(
            boot_id=_boot_id(),
            agent_binary_sha256=AgentUpdater.running(self.state_dir).binary_sha256(),
            slot=slot,
        )
        if record != self._reserve_worker:
            write_json_atomic(self.reserve_worker_path, model_payload(record), permissions=0o600)
            self._reserve_worker = record
            self._reserve_worker_on_disk = True

    def forget_reserve_worker(self) -> None:
        if not self._reserve_worker_on_disk:
            return
        self.reserve_worker_path.unlink(missing_ok=True)
        self._reserve_worker = None
        self._reserve_worker_on_disk = False

    def reserve_worker(self, machine_id: str) -> AgentReserveWorker | None:
        try:
            record = AgentReserveWorker.model_validate_json(
                self.reserve_worker_path.read_text(encoding="utf-8")
            )
        except FileNotFoundError:
            self._reserve_worker_on_disk = False
            return None
        except (OSError, ValueError):
            LOGGER.warning("ignoring the unreadable reserve worker record", exc_info=True)
            self.forget_reserve_worker()
            return None
        if record.slot.machine_id != machine_id:
            return None
        self._reserve_worker = record
        return record

    def reserve_prepared_in_earlier_boot(self) -> bool:
        """A changed boot ID proves the reserve restarted even if its provider row lags."""
        return self._reserve_worker is not None and self._reserve_worker.boot_id != _boot_id()

    def start_reserve_worker(
        self, record: AgentReserveWorker, bootstrap: AgentBootstrap
    ) -> AgentWorkerSlot | None:
        """Reuse a running reserve, or start its saved slot after a reboot with the same agent."""
        slot = record.slot.model_copy(update={"status": AgentWorkerSlotStatus.Active})
        if self._running_slot(slot) is not None:
            return slot
        if (
            record.boot_id == _boot_id()
            or record.agent_binary_sha256 != AgentUpdater.running(self.state_dir).binary_sha256()
        ):
            return None
        if slot.worker_image not in self._images.known():
            if not self._image_present(slot.worker_image):
                return None
            self._images.mark_prepared(slot.worker_image)
        expired = Event()
        timer = Timer(BOOT_WORKER_START_SECONDS, expired.set)
        timer.start()
        try:
            self._start(slot, bootstrap, reserve=True, stop=expired)
        finally:
            timer.cancel()
        return slot

    def waiting_for_admission(self, slots: Collection[AgentWorkerSlot]) -> list[str]:
        """The held workers that are built and wait only for their first call to pass."""
        return [
            slot.worker_id
            for slot in slots
            if Path(
                build_agent_worker_dirs(str(self.state_dir), slot.worker_id).tmp,
                WORKER_ADMISSION_WAITING_FILE,
            ).exists()
        ]

    def stop_reserve_worker(self, slot: AgentWorkerSlot) -> None:
        self._stop(slot)
        self._save_active_slots(
            [item for item in self._recorded_slots() if item.worker_id != slot.worker_id]
        )

    @property
    def active_slots_path(self) -> Path:
        return self.state_dir / AGENT_ACTIVE_SLOTS_FILE

    def observe_workers(self) -> list[WorkerObservation]:
        return [
            running
            for slot in self._recorded_slots()
            if (running := self._running_slot(slot)) is not None
        ]

    def _recorded_slots(self) -> list[AgentWorkerSlot]:
        with self._slots_lock:
            return self._read_slots()

    def _read_slots(self) -> list[AgentWorkerSlot]:
        if not self.active_slots_path.exists():
            return []
        raw = _JSON_VALUE_ADAPTER.validate_json(self.active_slots_path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        return [AgentWorkerSlot.model_validate(item) for item in raw]

    def apply(
        self,
        plan: AgentWorkerReconcilePlan,
        bootstrap: AgentBootstrap,
        *,
        active_slots: Sequence[AgentWorkerSlot],
        reported_images: Collection[str],
        reserve: bool = False,
    ) -> list[AgentWorkerReconcileAction]:
        """Apply the stream plan using its observed slots and reported images.

        Images prepared during this call must be reported on the next stream before
        starting their workers. Reserve workers start behind the admission hold."""
        active_by_id = {slot.worker_id: slot for slot in active_slots}
        applied: list[AgentWorkerReconcileAction] = []
        prepared: set[str] = set()
        # One wait per call, shared by every action, so a slow pull holds the
        # stream for at most WORKER_IMAGE_CHECK_WAIT_SECONDS however many slots it has.
        wait_until = time.monotonic() + WORKER_IMAGE_CHECK_WAIT_SECONDS
        for action in plan.actions:
            if action.action not in {
                WorkerSlotAction.Prepare,
                WorkerSlotAction.Start,
                WorkerSlotAction.Restart,
            }:
                continue
            if action.slot is None:
                continue
            image = action.slot.worker_image or self.worker_image_override
            if not image:
                raise ValueError(f"worker image is required for slot {action.worker_id}")
            if (
                self._images.ensure(image, wait_seconds=max(wait_until - time.monotonic(), 0.0))
                and image in reported_images
            ):
                prepared.add(action.worker_id)
        for action in plan.actions:
            if (
                action.action
                in {WorkerSlotAction.Prepare, WorkerSlotAction.Start, WorkerSlotAction.Restart}
                and action.worker_id not in prepared
            ):
                continue
            if action.action is WorkerSlotAction.Prepare:
                applied.append(action)
                continue
            if action.action in {WorkerSlotAction.Stop, WorkerSlotAction.Restart}:
                slot = active_by_id.pop(action.worker_id, None) or action.slot
                if slot is not None:
                    self._stop(slot)
                    applied.append(action)
            if action.action in {WorkerSlotAction.Start, WorkerSlotAction.Restart}:
                if action.slot is None:
                    continue
                self._start(action.slot, bootstrap, reserve=reserve)
                active_by_id[action.worker_id] = action.slot
                applied.append(action)
        self._reap_forgotten_workers(set(active_by_id))
        self._save_active_slots(list(active_by_id.values()))
        return applied

    def stop_all(self) -> None:
        """Remove every worker for good, as the machine is interrupted or the agent leaves."""
        self._halted.set()
        self._stop_recorded()

    def _stop_recorded(self) -> None:
        for slot in self._recorded_slots():
            self._stop(slot)
        self._save_active_slots([])

    def stop_for_reserve(self, machine_id: str) -> None:
        # A used machine's worker credential is retired as it stops, so it resumes
        # through the stream alone. It does not halt: a stop that is superseded
        # leaves the machine serving.
        self.forget_reserve_worker()
        self._stop_recorded()
        remaining = self.runner.run(
            [
                self.docker_binary,
                "ps",
                "--all",
                "--quiet",
                "--filter",
                f"label={NAME}.agent.machine_id={machine_id}",
            ]
        )
        if remaining.returncode or remaining.stdout.strip():
            raise RuntimeError("machine worker containers have not finished stopping")

    def gracefully_stop_all(self, *, grace_seconds: float) -> None:
        """Stop every worker within `grace_seconds`, for good, beside a stream still running."""
        if grace_seconds <= 0:
            raise ValueError("worker shutdown grace must be positive")
        self._halted.set()
        slots = self._recorded_slots()
        if not slots:
            self._save_active_slots([])
            return
        names = [f"{AGENT_NAME}-{sanitize_worker_name(slot.worker_id)}" for slot in slots]
        stop_result = self.runner.run(
            [
                self.docker_binary,
                "stop",
                "--timeout",
                str(math.ceil(grace_seconds)),
                *names,
            ]
        )
        remove_result = self.runner.run([self.docker_binary, "rm", "-f", *names])
        if remove_result.returncode != 0:
            msg = f"remove stopped workers failed: {remove_result.stderr or remove_result.stdout}"
            raise RuntimeError(msg)
        self._save_active_slots([])
        if stop_result.returncode != 0:
            msg = f"graceful worker shutdown failed: {stop_result.stderr or stop_result.stdout}"
            raise RuntimeError(msg)

    def _start(
        self,
        slot: AgentWorkerSlot,
        bootstrap: AgentBootstrap,
        *,
        reserve: bool = False,
        stop: Event | None = None,
    ) -> None:
        image = slot.worker_image or self.worker_image_override
        if not image:
            msg = f"worker image is required for slot {slot.worker_id}"
            raise ValueError(msg)
        if image not in self._images.prepared():
            raise RuntimeError(f"worker image is not prepared for slot {slot.worker_id}")
        if self._halted.is_set():
            raise RuntimeError("the machine's workers were shut down")
        plan = plan_worker_container(
            bootstrap,
            slot,
            state_dir=str(self.state_dir),
            image=image,
            agent_binary_sha256=AgentUpdater.running(self.state_dir).binary_sha256(),
            platform=self.platform,
            host_aliases=self.host_aliases,
            network=self.worker_network,
            disk_volume_slots=(
                self.disk_volume_slots() if self.disk_volume_slots is not None else None
            ),
            reserve=reserve,
        )
        for path in plan.dirs.all_paths():
            Path(path).mkdir(parents=True, exist_ok=True)
        Path(plan.dirs.tmp, WORKER_ADMISSION_WAITING_FILE).unlink(missing_ok=True)
        _write_worker_configuration_atomic(
            Path(plan.config_path),
            plan.config,
            permissions=0o600,
        )
        self._run_container(plan, stop=stop)
        # The shutdown halts before it reads the slot file under this lock, so a
        # worker is either recorded in time for the shutdown to stop it or sees
        # the halt here and removes itself.
        with self._slots_lock:
            halted = self._halted.is_set()
            if not halted:
                recorded = [item for item in self._read_slots() if item.worker_id != slot.worker_id]
                self._write_slots([*recorded, slot])
        if halted:
            self._docker([self.docker_binary, "rm", "-f", plan.name])
            raise RuntimeError("the machine's workers were shut down while one started")

    def _run_container(self, plan: AgentWorkerContainerPlan, *, stop: Event | None) -> None:
        timings = StepTimings()
        with timings.step("collect_exit"):
            self._collect_worker_exit(plan.name, plan.slot.worker_id)
        with timings.step("remove"):
            self._docker([self.docker_binary, "rm", "-f", plan.name])
        args = [self.docker_binary, plan.docker_args[0], "--detach", *plan.docker_args[1:]]
        with timings.step("run"):
            result = self._docker(args, stop=stop)
        timings.log(
            LOGGER,
            "worker %s container started at boot+%.2fs",
            plan.slot.worker_id,
            seconds_since_boot(),
        )
        if result.returncode != 0:
            detail = result.stderr or result.stdout
            raise RuntimeError(f"start worker slot {plan.slot.worker_id} failed: {detail}")

    def _stop(self, slot: AgentWorkerSlot) -> None:
        name = f"{AGENT_NAME}-{sanitize_worker_name(slot.worker_id)}"
        self._collect_worker_exit(name, slot.worker_id)
        result = self.runner.run([self.docker_binary, "rm", "-f", name])
        if result.returncode != 0 and not _slot_removal_is_settled(result.stderr or result.stdout):
            msg = f"stop worker slot {slot.worker_id} failed: {result.stderr or result.stdout}"
            raise RuntimeError(msg)

    def _reap_forgotten_workers(self, active_worker_ids: set[str]) -> None:
        """Remove exited containers whose slots no longer appear in the control-plane plan."""
        listed = self._docker(
            [
                self.docker_binary,
                "ps",
                "--all",
                "--filter",
                f"label={AGENT_MANAGED_LABEL}=true",
                "--format",
                '{{.Names}}\t{{.State}}\t{{.Label "' + AGENT_WORKER_ID_LABEL + '"}}',
            ]
        )
        if listed.returncode != 0:
            LOGGER.warning("listing managed worker containers failed: %s", listed.stderr.strip())
            return
        for line in listed.stdout.splitlines():
            name, _, remainder = line.partition("\t")
            state, _, worker_id = remainder.partition("\t")
            if not name or state.strip().lower() == "running":
                continue
            if worker_id and worker_id in active_worker_ids:
                continue
            self._collect_worker_exit(name, worker_id)
            self._docker([self.docker_binary, "rm", "-f", name])

    def _collect_worker_exit(self, name: str, worker_id: str) -> None:
        """Collect failed worker logs before removal; skip running workers being stopped."""
        inspected = self.runner.run(
            [
                self.docker_binary,
                "inspect",
                "-f",
                "{{.State.Running}} {{.State.ExitCode}} {{.State.OOMKilled}}",
                name,
            ]
        )
        if inspected.returncode != 0:
            return
        running, _, state = inspected.stdout.strip().partition(" ")
        if running.lower() != "false":
            return
        exit_code, _, oom_killed = state.partition(" ")
        LOGGER.warning(
            "worker %s exited (code %s, oom_killed %s)",
            worker_id or name,
            exit_code or "unknown",
            oom_killed or "unknown",
        )
        logs = self.runner.run(
            [self.docker_binary, "logs", "--tail", str(WORKER_EXIT_LOG_LINES), name]
        )
        for line in (f"{logs.stdout}\n{logs.stderr}").splitlines():
            if not line.strip():
                continue
            LOGGER.warning("worker %s: %s", worker_id or name, line)
            if self.report_worker_exit is not None:
                self.report_worker_exit(worker_id, line)

    def _running_slot(self, slot: AgentWorkerSlot) -> WorkerObservation | None:
        name = f"{AGENT_NAME}-{sanitize_worker_name(slot.worker_id)}"
        template = (
            "{{.State.Running}}\n{{range .Config.Env}}"
            '{{if eq (index (split . "=") 0) "WORKER_AGENT_BINARY_SHA256"}}'
            '{{index (split . "=") 1}}{{end}}{{end}}\n{{.HostConfig.NetworkMode}}\n'
            '{{range .Config.Env}}{{if eq (index (split . "=") 0) "'
            + WORKER_ADMISSION_HOLD_ENV
            + '"}}held{{end}}{{end}}'
        )
        result = self._docker([self.docker_binary, "inspect", "-f", template, name])
        running, _, remaining = result.stdout.strip().partition("\n")
        digest, _, remaining = remaining.partition("\n")
        namespace, _, hold = remaining.partition("\n")
        if result.returncode != 0 or running != "true":
            return None
        expected_namespace = self.worker_network.name
        if expected_namespace.startswith("container:"):
            owner = self._docker(
                [
                    self.docker_binary,
                    "inspect",
                    "-f",
                    "{{.Id}}",
                    expected_namespace.removeprefix("container:"),
                ]
            )
            if owner.returncode != 0 or not owner.stdout.strip():
                raise RuntimeError("Agent worker network namespace owner is unavailable")
            owner_id = owner.stdout.strip()
            expected_namespace = f"container:{owner_id}"
            if namespace == expected_namespace and (
                self._container_network_namespace(name)
                != self._container_network_namespace(owner_id)
            ):
                LOGGER.info(
                    "Worker %s is attached to a previous agent network namespace", slot.worker_id
                )
                return None
        if namespace != expected_namespace:
            LOGGER.info("Worker %s requires the current agent network namespace", slot.worker_id)
            return None
        observed = slot.model_copy()
        observed.agent_binary_sha256 = digest
        return WorkerObservation(observed, admission_held=hold == "held")

    def _container_network_namespace(self, container: str) -> str:
        result = self._docker(
            [self.docker_binary, "exec", container, "readlink", "/proc/self/ns/net"]
        )
        namespace = result.stdout.strip()
        if (
            result.returncode != 0
            or not namespace.startswith("net:[")
            or not namespace.endswith("]")
            or not namespace[5:-1].isdigit()
        ):
            raise RuntimeError(f"Cannot verify network namespace for container {container}")
        return namespace

    def _save_active_slots(self, slots: list[AgentWorkerSlot]) -> None:
        with self._slots_lock:
            self._write_slots([] if self._halted.is_set() else slots)

    def _write_slots(self, slots: list[AgentWorkerSlot]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload: list[JsonValue] = [
            model_payload(slot) for slot in sorted(slots, key=lambda item: item.worker_id)
        ]
        write_json_atomic(self.active_slots_path, payload, permissions=0o600)


def _boot_id() -> str:
    """This boot's identity, or empty where the kernel does not publish one."""
    try:
        return BOOT_ID_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_worker_configuration_atomic(
    path: Path,
    config: WorkerConfiguration,
    *,
    permissions: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with tmp_path.open("w", encoding="utf-8") as file:
            file.write(serialize_worker_configuration(config))
            file.flush()
            os.fsync(file.fileno())
        tmp_path.chmod(permissions)
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)
