from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import networking.tailnet as tailnet_runtime_module
import pytest
from networking.tailnet import (
    TailnetAuthenticationRequired,
    TailnetCommandResult,
    TailnetRuntime,
    TailnetRuntimeMode,
    TailnetRuntimeOptions,
)
from pydantic import JsonValue, SecretStr, TypeAdapter

_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


@dataclass(slots=True)
class _Runner:
    status_payloads: list[str] = field(default_factory=list)
    status_returncodes: list[int] = field(default_factory=list)
    status_stderr: str = ""
    up_returncodes: list[int] = field(default_factory=list)
    up_stderr: str = ""
    down_returncodes: list[int] = field(default_factory=list)
    down_stderr: str = ""
    calls: list[list[str]] = field(default_factory=list)
    auth_key_payloads: list[str] = field(default_factory=list)

    def run(
        self,
        args: list[str],
        *,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
    ) -> TailnetCommandResult:
        _ = timeout_seconds, env
        self.calls.append(args)
        if "up" in args:
            auth_arg = next(
                (item for item in args if item.startswith("--auth-key=file:")),
                None,
            )
            if auth_arg is not None:
                auth_path = Path(auth_arg.removeprefix("--auth-key=file:"))
                self.auth_key_payloads.append(auth_path.read_text(encoding="utf-8").strip())
            returncode = self.up_returncodes.pop(0) if self.up_returncodes else 0
            return TailnetCommandResult(returncode=returncode, stderr=self.up_stderr)
        if "down" in args:
            returncode = self.down_returncodes.pop(0) if self.down_returncodes else 0
            return TailnetCommandResult(returncode=returncode, stderr=self.down_stderr)
        if "status" in args:
            payload = self.status_payloads.pop(0) if self.status_payloads else _status_payload()
            returncode = self.status_returncodes.pop(0) if self.status_returncodes else 0
            return TailnetCommandResult(
                returncode=returncode,
                stdout=payload,
                stderr=self.status_stderr,
            )
        return TailnetCommandResult(returncode=0)


@dataclass(slots=True)
class _ManagedProcess:
    terminated: bool = False

    def poll(self) -> int | None:
        return None if not self.terminated else 0

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        _ = timeout
        self.terminated = True
        return 0


@dataclass(slots=True)
class _Launcher:
    calls: list[list[str]] = field(default_factory=list)
    log_paths: list[Path | None] = field(default_factory=list)

    def start(self, args: list[str], *, log_path: Path | None = None) -> _ManagedProcess:
        self.calls.append(args)
        self.log_paths.append(log_path)
        return _ManagedProcess()


def test_sidecar_tailnet_runtime_verifies_status_without_login(tmp_path: Path) -> None:
    runner = _Runner(status_payloads=[_status_payload()])
    runtime = TailnetRuntime(
        TailnetRuntimeOptions(
            mode=TailnetRuntimeMode.Sidecar,
            state_dir=str(tmp_path),
            socket_path="/run/tailscaled.sock",
            login_timeout_seconds=0.001,
            wait_poll_seconds=0.001,
        ),
        runner=runner,
    )

    runtime.start()
    runtime.start()

    assert not [call for call in runner.calls if "up" in call]
    assert not runner.auth_key_payloads
    status_calls = [call for call in runner.calls if "status" in call]
    assert len(status_calls) == 1
    assert "--socket=/run/tailscaled.sock" in status_calls[0]


def test_sidecar_tailnet_runtime_rejects_unauthenticated_status(tmp_path: Path) -> None:
    runtime = TailnetRuntime(
        TailnetRuntimeOptions(
            mode=TailnetRuntimeMode.Sidecar,
            state_dir=str(tmp_path),
            socket_path="/run/tailscaled.sock",
            login_timeout_seconds=0.001,
            wait_poll_seconds=0.001,
        ),
        runner=_Runner(
            status_payloads=[_status_payload(backend_state="NeedsLogin", self_tailnet_ips=[])] * 10,
        ),
    )

    with pytest.raises(RuntimeError, match="not authenticated"):
        runtime.start()


def test_managed_tailnet_runtime_uses_auth_key_file_and_login_server(tmp_path: Path) -> None:
    runner = _Runner(
        status_payloads=[
            _status_payload(backend_state="NeedsLogin", self_tailnet_ips=[]),
            _status_payload(backend_state="NeedsLogin", self_tailnet_ips=[]),
            _status_payload(),
        ]
    )
    launcher = _Launcher()
    runtime = TailnetRuntime(
        TailnetRuntimeOptions(
            mode=TailnetRuntimeMode.Managed,
            hostname="gateway-one",
            auth_key=SecretStr("test-tailnet-auth-key"),
            control_url="https://headscale.example",
            state_dir=str(tmp_path),
            socket_path="/run/tailscaled.sock",
        ),
        runner=runner,
        launcher=launcher,
    )

    runtime.start()
    runtime.start()

    up_calls = [call for call in runner.calls if "up" in call]
    assert len(up_calls) == 1
    assert runner.auth_key_payloads == ["test-tailnet-auth-key"]
    assert not list(tmp_path.glob(".tailscale-auth-*"))
    up_call = up_calls[0]
    assert "test-tailnet-auth-key" not in " ".join(up_call)
    assert "--socket=/run/tailscaled.sock" in up_call
    assert "--hostname=gateway-one" in up_call
    assert "--login-server=https://headscale.example" in up_call


def test_managed_tailnet_runtime_refuses_stopped_identity_without_stable_node_id(
    tmp_path: Path,
) -> None:
    runtime = TailnetRuntime(
        TailnetRuntimeOptions(mode=TailnetRuntimeMode.Managed, state_dir=str(tmp_path)),
        runner=_Runner(
            status_payloads=[
                _status_payload(
                    backend_state="Stopped",
                    self_node_id="",
                ),
                _status_payload(
                    backend_state="Stopped",
                    self_node_id="",
                ),
            ]
        ),
        launcher=_Launcher(),
    )

    with pytest.raises(RuntimeError, match="no stable node ID") as exc:
        runtime.start()

    assert not isinstance(exc.value, TailnetAuthenticationRequired)


def test_tailnet_runtime_retries_up_and_redacts_auth_key(tmp_path: Path) -> None:
    runner = _Runner(
        status_payloads=[
            _status_payload(backend_state="NeedsLogin", self_tailnet_ips=[]),
            _status_payload(backend_state="NeedsLogin", self_tailnet_ips=[]),
            _status_payload(),
        ],
        up_returncodes=[1, 0],
        up_stderr="failed with test-tailnet-auth-key",
    )
    launcher = _Launcher()
    runtime = TailnetRuntime(
        TailnetRuntimeOptions(
            mode=TailnetRuntimeMode.Managed,
            hostname="gateway-one",
            auth_key=SecretStr("test-tailnet-auth-key"),
            state_dir=str(tmp_path),
            wait_poll_seconds=0.001,
        ),
        runner=runner,
        launcher=launcher,
    )

    runtime.start()

    assert len([call for call in runner.calls if "up" in call]) == 2
    assert not list(tmp_path.glob(".tailscale-auth-*"))

    failing_runtime = TailnetRuntime(
        TailnetRuntimeOptions(
            mode=TailnetRuntimeMode.Managed,
            hostname="gateway-one",
            auth_key=SecretStr("test-tailnet-auth-key"),
            state_dir=str(tmp_path),
            login_timeout_seconds=0.001,
            wait_poll_seconds=0.001,
        ),
        runner=_Runner(
            status_payloads=[
                _status_payload(backend_state="NeedsLogin", self_tailnet_ips=[]),
                _status_payload(backend_state="NeedsLogin", self_tailnet_ips=[]),
            ],
            up_returncodes=[1] * 100,
            up_stderr="failed with test-tailnet-auth-key",
        ),
        launcher=_Launcher(),
    )
    with pytest.raises(RuntimeError) as exc:
        failing_runtime.start()
    assert "test-tailnet-auth-key" not in str(exc.value)
    assert "<redacted-tailnet-key>" in str(exc.value)


def test_wait_for_peer_polls_until_tailnet_peer_is_reachable(tmp_path: Path) -> None:
    runner = _Runner(
        status_payloads=[
            _status_payload(peer_online=False, peer_current_address=""),
            _status_payload(peer_online=True, peer_current_address="203.0.113.10:41641"),
        ]
    )
    runtime = TailnetRuntime(
        TailnetRuntimeOptions(
            state_dir=str(tmp_path),
            socket_path="/run/tailscaled.sock",
            wait_poll_seconds=0.001,
        ),
        runner=runner,
    )

    runtime.wait_for_peer("agent-one.tailnet.example", timeout_seconds=1)

    assert len([call for call in runner.calls if "status" in call]) == 2


def test_resolve_peer_host_prefers_online_duplicate_peer(tmp_path: Path) -> None:
    payload = _JSON_OBJECT_ADAPTER.validate_json(
        _status_payload(peer_online=False, peer_current_address="")
    )
    peers = payload.get("Peer")
    assert isinstance(peers, dict)
    first_peer = peers.get("nodekey:peer")
    assert isinstance(first_peer, dict)
    peers["nodekey:online"] = {
        "HostName": "agent-one",
        "DNSName": "agent-one.tailnet.example.",
        "TailscaleIPs": ["100.64.0.42"],
        "Online": True,
        "Active": False,
        "CurAddr": "203.0.113.42:41641",
        "Relay": "",
        "LastHandshake": "2026-01-01T00:00:00Z",
    }
    runtime = TailnetRuntime(
        TailnetRuntimeOptions(
            state_dir=str(tmp_path),
            socket_path="/run/tailscaled.sock",
        ),
        runner=_Runner(status_payloads=[json.dumps(payload), json.dumps(payload)]),
    )

    assert runtime.resolve_peer_host("agent-one.tailnet.example") == "100.64.0.42"


def test_wait_for_peer_times_out_when_peer_is_missing(tmp_path: Path) -> None:
    runtime = TailnetRuntime(
        TailnetRuntimeOptions(
            state_dir=str(tmp_path),
            socket_path="/run/tailscaled.sock",
            wait_poll_seconds=0.001,
        ),
        runner=_Runner(status_payloads=[_status_payload(peer_name="other")] * 100),
    )

    with pytest.raises(TimeoutError, match="tailnet peer agent-one"):
        runtime.wait_for_peer("agent-one", timeout_seconds=0.002)


@pytest.mark.parametrize("mode", [TailnetRuntimeMode.Managed, TailnetRuntimeMode.Sidecar])
def test_repeated_missing_peer_refreshes_control_session_without_changing_identity(
    tmp_path: Path,
    mode: TailnetRuntimeMode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tailnet_runtime_module,
        "TAILNET_STALE_PEER_MISS_WINDOW_SECONDS",
        0.001,
    )
    runner = _Runner(
        status_payloads=[
            _status_payload(
                peer_name="other",
                peer_online=False,
                peer_current_address="",
            )
        ]
        * 100,
        up_returncodes=[1, 0],
    )
    launcher = _Launcher()
    runtime = TailnetRuntime(
        TailnetRuntimeOptions(
            mode=mode,
            hostname="gateway-one",
            auth_key=SecretStr("test-tailnet-auth-key"),
            state_dir=str(tmp_path),
            socket_path="/run/tailscaled.sock",
            wait_poll_seconds=0.001,
        ),
        runner=runner,
        launcher=launcher,
    )
    runtime.start()

    for attempt in range(3):
        with pytest.raises(TimeoutError) as exc:
            runtime.wait_for_peer("agent-one", timeout_seconds=0.002)
        if attempt == 2:
            assert "tailnet control session refreshed" in str(exc.value)

    runner.status_payloads = [_status_payload(peer_online=True)]
    runtime.wait_for_peer("agent-one", timeout_seconds=1)

    assert len([call for call in runner.calls if "down" in call]) == 1
    reconnects = [call for call in runner.calls if "up" in call and len(call) <= 3]
    assert len(reconnects) == 2
    assert not runner.auth_key_payloads
    assert runtime.status().self_node_id == "node-self"
    assert len(launcher.calls) == (1 if mode is TailnetRuntimeMode.Managed else 0)


@pytest.mark.parametrize("mode", [TailnetRuntimeMode.Managed, TailnetRuntimeMode.Sidecar])
def test_failed_refresh_is_retried_by_later_start_without_auth_key(
    tmp_path: Path,
    mode: TailnetRuntimeMode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tailnet_runtime_module,
        "TAILNET_STALE_PEER_MISS_WINDOW_SECONDS",
        0.001,
    )
    runner = _Runner(
        status_payloads=[
            _status_payload(
                peer_name="other",
                peer_online=False,
                peer_current_address="",
            )
        ]
        * 100,
        up_returncodes=[1] * 1000,
        up_stderr="temporary reconnect failure",
    )
    runtime = TailnetRuntime(
        TailnetRuntimeOptions(
            mode=mode,
            state_dir=str(tmp_path),
            socket_path="/run/tailscaled.sock",
            login_timeout_seconds=0.001,
            wait_poll_seconds=0.001,
        ),
        runner=runner,
        launcher=_Launcher(),
    )
    runtime.start()
    for _ in range(2):
        with pytest.raises(TimeoutError):
            runtime.wait_for_peer("agent-one", timeout_seconds=0.001)
    with pytest.raises(TimeoutError, match="control-session refresh failed"):
        runtime.wait_for_peer("agent-one", timeout_seconds=0.001)
    failed_reconnect_count = len([call for call in runner.calls if "up" in call])

    runner.up_returncodes = [0]
    runner.status_payloads = [
        _status_payload(backend_state="Stopped", self_node_id=""),
        _status_payload(),
        _status_payload(),
    ]
    recovered = runtime.status()

    assert len([call for call in runner.calls if "up" in call]) == failed_reconnect_count + 1
    assert not runner.auth_key_payloads
    assert recovered.self_node_id == "node-self"


@pytest.mark.parametrize("mode", [TailnetRuntimeMode.Managed, TailnetRuntimeMode.Sidecar])
def test_new_runtime_recovers_stopped_reusable_identity_keylessly(
    tmp_path: Path,
    mode: TailnetRuntimeMode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tailnet_runtime_module,
        "TAILNET_STALE_PEER_MISS_WINDOW_SECONDS",
        0.001,
    )
    first_runner = _Runner(
        status_payloads=[
            _status_payload(
                peer_name="other",
                peer_online=False,
                peer_current_address="",
            )
        ]
        * 100,
        up_returncodes=[1] * 1000,
        up_stderr="temporary reconnect failure",
    )
    first = TailnetRuntime(
        TailnetRuntimeOptions(
            mode=mode,
            state_dir=str(tmp_path),
            socket_path="/run/tailscaled.sock",
            login_timeout_seconds=0.001,
            wait_poll_seconds=0.001,
        ),
        runner=first_runner,
        launcher=_Launcher(),
    )
    first.start()
    for _ in range(2):
        with pytest.raises(TimeoutError):
            first.wait_for_peer("agent-one", timeout_seconds=0.001)
    with pytest.raises(TimeoutError, match="control-session refresh failed"):
        first.wait_for_peer("agent-one", timeout_seconds=0.001)
    reconnect_marker = tmp_path / ".reconnect-node-id"
    assert json.loads(reconnect_marker.read_text(encoding="utf-8")) == {
        "node_id": "node-self",
        "down_completed": True,
    }

    stopped_statuses = 2 if mode is TailnetRuntimeMode.Managed else 1
    runner = _Runner(
        status_payloads=[_status_payload(backend_state="Stopped", self_node_id="")]
        * stopped_statuses
        + [_status_payload(), _status_payload()]
    )
    runtime = TailnetRuntime(
        TailnetRuntimeOptions(
            mode=mode,
            state_dir=str(tmp_path),
            socket_path="/run/tailscaled.sock",
            login_timeout_seconds=0.001,
            wait_poll_seconds=0.001,
        ),
        runner=runner,
        launcher=_Launcher(),
    )

    recovered = runtime.status()

    assert len([call for call in runner.calls if "up" in call]) == 1
    assert not runner.auth_key_payloads
    assert recovered.self_node_id == "node-self"
    assert not reconnect_marker.exists()


@pytest.mark.parametrize("mode", [TailnetRuntimeMode.Managed, TailnetRuntimeMode.Sidecar])
def test_cold_start_completes_refresh_interrupted_before_down(
    tmp_path: Path,
    mode: TailnetRuntimeMode,
) -> None:
    interrupted = TailnetRuntime(
        TailnetRuntimeOptions(
            mode=mode,
            state_dir=str(tmp_path),
            socket_path="/run/tailscaled.sock",
        ),
        runner=_Runner(),
    )
    interrupted._set_reconnect_expected_node_id("node-self", down_completed=False)
    running_statuses = 2 if mode is TailnetRuntimeMode.Managed else 1
    runner = _Runner(
        status_payloads=[_status_payload()] * running_statuses
        + [_status_payload(), _status_payload()]
    )
    recovered = TailnetRuntime(
        interrupted.options,
        runner=runner,
        launcher=_Launcher(),
    )

    status = recovered.status()

    assert len([call for call in runner.calls if "down" in call]) == 1
    assert len([call for call in runner.calls if "up" in call]) == 1
    assert not runner.auth_key_payloads
    assert status.self_node_id == "node-self"
    assert not (tmp_path / ".reconnect-node-id").exists()


def test_peer_recovery_requires_sustained_misses_and_no_unrelated_active_traffic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = TailnetRuntime(
        TailnetRuntimeOptions(
            mode=TailnetRuntimeMode.Sidecar,
            state_dir=str(tmp_path),
            socket_path="/run/tailscaled.sock",
        ),
        runner=_Runner(),
    )
    timestamps = iter([0.0, 1.0, 2.0, 60.0, 61.0, 120.0, 121.0, 122.0, 182.0, 183.0])
    monkeypatch.setattr(tailnet_runtime_module.time, "monotonic", lambda: next(timestamps))

    assert not runtime._record_peer_miss(
        "missing-one",
        unrelated_peer_reachable=False,
    )
    assert not runtime._record_peer_miss(
        "missing-one",
        unrelated_peer_reachable=False,
    )
    assert not runtime._record_peer_miss(
        "missing-one",
        unrelated_peer_reachable=False,
    )
    assert not runtime._record_peer_miss(
        "missing-active",
        unrelated_peer_reachable=True,
    )
    assert not runtime._record_peer_miss(
        "missing-sustained",
        unrelated_peer_reachable=False,
    )
    assert not runtime._record_peer_miss(
        "missing-sustained",
        unrelated_peer_reachable=False,
    )
    assert runtime._record_peer_miss(
        "missing-sustained",
        unrelated_peer_reachable=False,
    )
    assert not runtime._record_peer_miss(
        "missing-concurrent",
        unrelated_peer_reachable=False,
    )
    assert not runtime._record_peer_miss(
        "missing-concurrent",
        unrelated_peer_reachable=False,
    )
    assert not runtime._record_peer_miss(
        "missing-concurrent",
        unrelated_peer_reachable=False,
    )


def test_managed_tailnet_runtime_restart_reuses_authenticated_state_without_key(
    tmp_path: Path,
) -> None:
    first_runner = _Runner(
        status_payloads=[
            _status_payload(backend_state="NeedsLogin", self_tailnet_ips=[]),
            _status_payload(backend_state="NeedsLogin", self_tailnet_ips=[]),
            _status_payload(backend_state="NeedsLogin", self_tailnet_ips=[]),
            _status_payload(),
        ]
    )
    first = TailnetRuntime(
        TailnetRuntimeOptions(mode=TailnetRuntimeMode.Managed, state_dir=str(tmp_path)),
        runner=first_runner,
        launcher=_Launcher(),
    )

    with pytest.raises(TailnetAuthenticationRequired):
        first.start()
    first.authenticate(auth_key="one-off-key", hostname="agent-one")
    first.close()

    restart_runner = _Runner(status_payloads=[_status_payload(), _status_payload()])
    restart = TailnetRuntime(
        TailnetRuntimeOptions(mode=TailnetRuntimeMode.Managed, state_dir=str(tmp_path)),
        runner=restart_runner,
        launcher=_Launcher(),
    )
    restart.start()

    assert not [call for call in restart_runner.calls if "up" in call]
    assert restart.status().self_node_id == "node-self"


def test_managed_tailnet_runtime_waits_for_persisted_identity_to_load(
    tmp_path: Path,
) -> None:
    runner = _Runner(
        status_payloads=[
            _status_payload(backend_state="NoState", self_tailnet_ips=[]),
            _status_payload(),
            _status_payload(),
        ]
    )
    runtime = TailnetRuntime(
        TailnetRuntimeOptions(
            mode=TailnetRuntimeMode.Managed,
            state_dir=str(tmp_path),
        ),
        runner=runner,
        launcher=_Launcher(),
    )

    runtime.start()

    assert runtime.status().self_node_id == "node-self"
    assert len([call for call in runner.calls if "status" in call]) == 4


def _status_payload(
    *,
    backend_state: str = "Running",
    self_tailnet_ips: list[str] | None = None,
    peer_name: str = "agent-one",
    peer_online: bool = True,
    peer_current_address: str = "203.0.113.10:41641",
    self_node_id: str = "node-self",
) -> str:
    self_ips = ["100.64.0.1"] if self_tailnet_ips is None else self_tailnet_ips
    return json.dumps(
        {
            "BackendState": backend_state,
            "Self": {
                "ID": self_node_id,
                "HostName": "gateway-one",
                "DNSName": "gateway-one.tailnet.example.",
                "Online": True,
                "Relay": "",
                "TailscaleIPs": self_ips,
            },
            "Peer": {
                "nodekey:peer": {
                    "HostName": peer_name,
                    "DNSName": f"{peer_name}.tailnet.example.",
                    "TailscaleIPs": ["100.64.0.2"],
                    "Online": peer_online,
                    "Active": False,
                    "CurAddr": peer_current_address,
                    "Relay": "",
                    "LastHandshake": "2026-01-01T00:00:00Z",
                }
            },
        }
    )
