from __future__ import annotations

import shlex
import signal


def build_pool_join_command(
    command: str,
    *,
    agent_bin: str = "",
    executor: str = "",
    worker_image: str = "",
    max_cpu: str = "",
    max_memory: str = "",
    max_gpus: int = 0,
    gpu_ids: str = "",
    background: bool | None = None,
    service_manager: str = "",
    service_name: str = "",
    state_dir: str = "",
) -> str:
    flags: list[str] = []
    if background is True:
        flags.append("--background")
    elif background is False:
        flags.append("--foreground")
    _append_flag(flags, "--service-manager", service_manager)
    _append_flag(flags, "--service-name", service_name)
    _append_flag(flags, "--state-dir", state_dir)
    _append_flag(flags, "--agent-bin", agent_bin)
    _append_flag(flags, "--executor", executor)
    _append_flag(flags, "--worker-image", worker_image)
    _append_flag(flags, "--max-cpu", max_cpu)
    _append_flag(flags, "--max-memory", max_memory)
    _append_positive_int_flag(flags, "--max-gpus", max_gpus)
    _append_flag(flags, "--gpu-ids", gpu_ids)
    return command if not flags else f"{command} {shlex.join(flags)}"


def agent_join_interrupted(exit_code: int) -> bool:
    return exit_code in (-signal.SIGINT, 128 + signal.SIGINT)


def _append_flag(flags: list[str], name: str, value: str) -> None:
    if not value:
        return
    flags.extend([name, value])


def _append_positive_int_flag(flags: list[str], name: str, value: int) -> None:
    if value <= 0:
        return
    flags.extend([name, str(value)])
