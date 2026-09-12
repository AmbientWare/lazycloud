"""Real Linux forwarding acceptance; requires Docker and the local tunnel-gateway image.

Run: uv run --group dev --group workspace python -m \\
    packages.networking.tests.acceptance.forwarded_gateway_failover
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from pydantic import TypeAdapter

_STATUS = TypeAdapter(dict[str, str | int])
_RESPONSES = TypeAdapter(list[str])


def run_case(draining_index: int) -> None:
    repo = Path(__file__).resolve().parents[4]
    surviving_index = 1 - draining_index
    prefix = "lzy-forward-proof-" + uuid.uuid4().hex[:8]
    root = Path(tempfile.mkdtemp(prefix=prefix))
    image = "tunnel-gateway:local"
    names = {
        role: prefix + "-" + role
        for role in ("gw0", "gw1", "agent", "platform", "origin", "workload")
    }
    created: list[str] = []
    networks: list[str] = []

    def run(*args: str, timeout: int = 30) -> str:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(f"{args[0]} failed: {result.stderr}")
        return result.stdout.strip()

    def execute(role: str, code: str) -> str:
        return run("docker", "exec", names[role], "python", "-c", code)

    def signals() -> dict[str, dict[str, str | int]]:
        state: dict[str, dict[str, str | int]] = {}
        for role in ("gw0", "gw1", "agent", "platform"):
            path = root / f"{role}.status"
            state[role] = (
                _STATUS.validate_json(path.read_text()) if path.exists() else {"status": "starting"}
            )
            log = root / f"{role}.log"
            if log.exists() and "Traceback" in log.read_text():
                raise RuntimeError(f"{role}: {log.read_text()}")
        print(json.dumps(state), flush=True)
        return state

    def requests(role: str, count: int = 6) -> list[str]:
        return _RESPONSES.validate_json(
            execute(
                role,
                "import urllib.request,json; print(json.dumps(["
                "urllib.request.urlopen('http://100.96.0.1:9000',timeout=2).read().decode() "
                "for _ in range(" + str(count) + ")]))",
            )
        )

    try:
        for network in (prefix, prefix + "-workload"):
            run("docker", "network", "create", network)
            networks.append(network)
        for role, name in names.items():
            network = networks[1] if role == "workload" else networks[0]
            args = [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--network",
                network,
                "--cap-add",
                "NET_ADMIN",
                "--sysctl",
                "net.ipv4.ip_forward=1",
                "--sysctl",
                "net.ipv4.conf.all.src_valid_mark=1",
                "-v",
                f"{root}:/proof",
                "-w",
                "/proof-module",
                "-v",
                f"{repo / 'packages/networking/tests/acceptance'}:/proof-module/acceptance:ro",
            ]
            for owner, path in [
                ("networking", "packages/networking/src/networking"),
                ("shared", "packages/shared/src/shared"),
                ("database", "packages/database/src/database"),
                ("tunnel_gateway_app", "apps/tunnel-gateway/src/tunnel_gateway_app"),
            ]:
                args += ["-v", f"{repo / path}:/app/.venv/lib/python3.12/site-packages/{owner}:ro"]
            args += [image, "python", "-c", "import time; time.sleep(3600)"]
            run(*args)
            created.append(name)
        run("docker", "network", "connect", networks[1], names["agent"])
        keys = {
            role: run(
                "docker",
                "exec",
                names[role],
                "python",
                "-m",
                "acceptance._gateway_node",
                "key",
                role,
            )
            for role in ("gw0", "gw1", "agent", "platform")
        }
        ips = {
            role: run(
                "docker",
                "inspect",
                "--format",
                '{{(index .NetworkSettings.Networks "' + networks[0] + '").IPAddress}}',
                name,
            )
            for role, name in names.items()
            if role != "workload"
        }
        workload_ip = run(
            "docker",
            "inspect",
            "--format",
            '{{(index .NetworkSettings.Networks "' + networks[1] + '").IPAddress}}',
            names["workload"],
        )
        router_ip = run(
            "docker",
            "inspect",
            "--format",
            '{{(index .NetworkSettings.Networks "' + networks[1] + '").IPAddress}}',
            names["agent"],
        )
        addresses = {"agent": "100.96.1.10/32", "platform": "100.96.0.2/32"}
        (root / "config.json").write_text(
            json.dumps(
                {
                    "origin": ips["origin"],
                    "peers": [
                        {"key": keys[role], "address": addresses[role]} for role in addresses
                    ],
                }
            )
        )
        for role in addresses:
            network = "100.96.0.0/24" if role == "agent" else "100.96.0.0/11"
            (root / f"{role}.json").write_text(
                json.dumps(
                    {
                        "peer_id": role,
                        "address": addresses[role],
                        "allowed_ips": [network],
                        "generation": 1,
                        "routes": [{"network": network, "gateway_indices": [0, 1]}],
                        "gateways": [
                            {
                                "index": index,
                                "public_key": keys[f"gw{index}"],
                                "endpoint": names[f"gw{index}"] + ":51820",
                            }
                            for index in range(2)
                        ],
                    }
                )
            )
        for role in names:
            run(
                "docker",
                "exec",
                "-d",
                names[role],
                "sh",
                "-c",
                f"exec python -m acceptance._gateway_node {role} > /proof/{role}.log 2>&1",
            )
        for _cycle in range(14):
            state = signals()
            if all(list(state[role].values()) == ["ready", "ready"] for role in addresses):
                break
            time.sleep(1)
        else:
            raise RuntimeError("Both encrypted gateway paths did not become usable")
        run(
            "docker",
            "exec",
            names["workload"],
            "ip",
            "route",
            "replace",
            "100.96.0.0/24",
            "via",
            router_ip,
        )
        run(
            "docker",
            "exec",
            names["agent"],
            "iptables",
            "-t",
            "nat",
            "-A",
            "POSTROUTING",
            "-s",
            workload_ip,
            "-o",
            "lzy-wg-+",
            "-j",
            "MASQUERADE",
        )
        run(
            "docker",
            "exec",
            names["agent"],
            "iptables",
            "-t",
            "nat",
            "-A",
            "PREROUTING",
            "-d",
            "100.96.1.10",
            "-p",
            "tcp",
            "--dport",
            "29444",
            "-j",
            "DNAT",
            "--to-destination",
            workload_ip + ":29443",
        )
        print(
            "Overlay routes across all tables:",
            run(
                "docker",
                "exec",
                names["agent"],
                "ip",
                "-4",
                "route",
                "show",
                "table",
                "all",
                "match",
                "100.96.0.1/32",
            ),
            flush=True,
        )
        initial = requests("workload", 12)
        print("Initial forwarded gateway addresses:", initial, flush=True)
        assert set(initial) == {ips["gw0"], ips["gw1"]}
        stream = (
            "import socket,time\nfrom pathlib import Path\n"
            "s=socket.socket(); s.settimeout(2); "
            "s.setsockopt(socket.SOL_SOCKET,socket.SO_MARK,"
            + str((draining_index + 1) << 16)
            + "); s.connect(('100.96.1.10',29444))\nfor i in range(200):\n"
            " s.sendall(b'live'); assert s.recv(4)==b'live'\n"
            " Path('/proof/stream.status').write_text(str(i)); time.sleep(.1)\ns.close()\n"
        )
        (root / "stream.py").write_text(stream)
        run(
            "docker",
            "exec",
            "-d",
            names["platform"],
            "sh",
            "-c",
            "exec python /proof/stream.py > /proof/stream.log 2>&1",
        )
        (root / f"gw{draining_index}.drain").touch()
        for _cycle in range(7):
            state = signals()
            if all(state[role].get(str(draining_index)) == "draining" for role in addresses):
                break
            time.sleep(1)
        else:
            raise RuntimeError("Clients did not observe the gateway drain")
        host = requests("agent")
        forwarded = requests("workload", 12)
        print(
            json.dumps(
                {
                    "draining_gateway": draining_index,
                    "host_requests": host,
                    "forwarded_requests": forwarded,
                }
            ),
            flush=True,
        )
        assert set(host) == {ips[f"gw{surviving_index}"]}
        assert set(forwarded) == {ips[f"gw{surviving_index}"]}
        before = int((root / "stream.status").read_text())
        time.sleep(0.5)
        after = int((root / "stream.status").read_text())
        assert after > before
        assert "Traceback" not in (root / "stream.log").read_text()
        print("Forwarded reply stream continued during drain:", before, after, flush=True)
        run(
            "docker",
            "exec",
            names["agent"],
            "iptables",
            "-I",
            "OUTPUT",
            "-p",
            "udp",
            "-d",
            ips[f"gw{draining_index}"],
            "--dport",
            "51820",
            "-j",
            "DROP",
        )
        assert set(requests("workload", 12)) == {ips[f"gw{surviving_index}"]}
        reverse = execute(
            "platform",
            "import socket; s=socket.socket(); s.settimeout(2); "
            "s.setsockopt(socket.SOL_SOCKET,socket.SO_MARK,"
            + str((surviving_index + 1) << 16)
            + "); s.connect(('100.96.1.10',29444)); s.sendall(b'reply'); "
            "assert s.recv(5)==b'reply'; "
            "print('platform-to-container reply returned through surviving gateway')",
        )
        print(reverse, flush=True)
        for role in addresses:
            run(
                "docker",
                "exec",
                names[role],
                "ip",
                "route",
                "add",
                "203.0.113.0/24",
                "dev",
                "eth0",
                "proto",
                "99",
            )
            run("docker", "exec", names[role], "kill", "-TERM", (root / f"{role}.pid").read_text())
        for _cycle in range(5):
            print(
                "Cleanup:",
                {role: (root / f"{role}.closed").exists() for role in addresses},
                flush=True,
            )
            if all((root / f"{role}.closed").exists() for role in addresses):
                break
            time.sleep(1)
        else:
            raise RuntimeError("Client cleanup did not finish")
        for role in addresses:
            assert (
                run("docker", "exec", names[role], "ip", "-o", "link", "show", "type", "wireguard")
                == ""
            )
            assert "LZY-WG-" not in run(
                "docker", "exec", names[role], "iptables", "-t", "mangle", "-S"
            )
            assert run("docker", "exec", names[role], "ip", "route", "show", "203.0.113.0/24")
        print("Tunnel hooks removed and unrelated routes preserved", flush=True)
    finally:
        for role in names:
            path = root / f"{role}.log"
            if path.exists() and "Traceback" in path.read_text():
                print(role, path.read_text(), flush=True)
        if created:
            run(
                "docker",
                "exec",
                created[-1],
                "chown",
                "-R",
                f"{os.getuid()}:{os.getgid()}",
                "/proof",
            )
        for name in created:
            run("docker", "rm", "--force", name)
        for network in reversed(networks):
            run("docker", "network", "rm", network)
        shutil.rmtree(root)
        print("Removed all proof containers, networks and temporary keys", flush=True)


def main() -> None:
    for index in (0, 1):
        run_case(index)


if __name__ == "__main__":
    main()
