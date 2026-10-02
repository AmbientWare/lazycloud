"""Write an isolated .env for a reference checkout from its own .env.example.

Usage: python ref_env.py <reference checkout> <state dir> [stripe env file]

Ports, the Compose project, image tag, agent state and AWS config move off the
defaults so the stack runs beside any other LazyCloud stack on the host. Only
LAZYCLOUD_STRIPE_API_KEY and LAZYCLOUD_STRIPE_WEBHOOK_SECRET are read from the
optional Stripe file; nothing else from another checkout is used.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PORT = 38000


def main() -> None:
    ref, state = Path(sys.argv[1]), Path(sys.argv[2])
    env = ref / ".env"
    if env.is_symlink():
        env.unlink()
    env.write_text((ref / ".env.example").read_text())
    env.chmod(0o600)
    subprocess.run([sys.executable, str(ref / "deploy/local_credentials.py"), str(env)], check=True)
    values = {
        "LAZYCLOUD_HOME": f"{state}/ref-home",
        "LAZYCLOUD_ENDPOINT": f"http://lazycloud.localhost:{PORT}",
        "LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL": f"http://lazycloud.localhost:{PORT}",
        "VITE_API_TARGET": f"http://127.0.0.1:{PORT}",
        "LAZYCLOUD_GITHUB_REDIRECT_URI": f"http://lazycloud.localhost:{PORT}/auth/github/callback",
        "LAZYCLOUD_DATABASE_URL": "postgresql+psycopg://lazycloud:lazycloud@localhost:36432/lazycloud",
        "LAZYCLOUD_DATABASE_DIRECT_URL": "postgresql+psycopg://lazycloud:lazycloud@localhost:35432/lazycloud",
        "LAZYCLOUD_REDIS_URL": "redis://localhost:36379/0",
        "LAZYCLOUD_OBJECT_STORE_ENDPOINT_URL": "http://object-store.localhost:33900",
        "LAZYCLOUD_GARAGE_ADMIN_ENDPOINT_URL": "http://127.0.0.1:33903",
        "LAZYCLOUD_COMPOSE_CONTROL_PLANE_PORT": str(PORT),
        "LAZYCLOUD_COMPOSE_TCP_INGRESS_PORT": "31995",
        "LAZYCLOUD_COMPOSE_POSTGRES_PORT": "35432",
        "LAZYCLOUD_COMPOSE_PGBOUNCER_PORT": "36432",
        "LAZYCLOUD_COMPOSE_REDIS_PORT": "36379",
        "LAZYCLOUD_COMPOSE_WORKLOAD_REGISTRY_PORT": "35000",
        "LAZYCLOUD_COMPOSE_OBJECT_STORE_PORT": "33900",
        "LAZYCLOUD_COMPOSE_OBJECT_STORE_ADMIN_PORT": "33903",
        "LAZYCLOUD_COMPOSE_IMAGE_TAG": "bench-ref",
        "LAZYCLOUD_COMPOSE_AGENT_MAX_MEMORY_MIB": "8192",
        "AWS_PROFILE": "lcbench-none",
        "COMPOSE_PROJECT_NAME": "lcbench-ref",
        "COMPOSE_FILE": "compose.yaml:compose.bench.yaml",
        "COMPOSE_PROFILES": "customer-compute",
        "LAZYCLOUD_COMPOSE_AGENT_STATE_DIR": f"{state}/ref-agent",
        "LAZYCLOUD_COMPOSE_AWS_CONFIG_DIR": f"{state}/ref-aws",
        "LAZYCLOUD_COMPOSE_AGENT_MAX_CPU_CORES": "4",
        "LAZYCLOUD_COMPOSE_AGENT_HOSTNAME": "lcbench-agent",
        "LAZYCLOUD_COMPOSE_AGENT_MACHINE_FINGERPRINT": "lcbench-agent",
    }
    if len(sys.argv) > 3:
        for line in Path(sys.argv[3]).read_text().splitlines():
            name, _, value = line.partition("=")
            if name in ("LAZYCLOUD_STRIPE_API_KEY", "LAZYCLOUD_STRIPE_WEBHOOK_SECRET"):
                values[name] = value.strip().strip("\"'")
    content = env.read_text()
    for name, value in values.items():
        pattern = rf"^{name}=.*$"
        if re.search(pattern, content, flags=re.MULTILINE):
            line = f"{name}={value}"
            content = re.sub(pattern, lambda _, line=line: line, content, flags=re.MULTILINE)
        else:
            content += f"\n{name}={value}\n"
    env.write_text(content)


if __name__ == "__main__":
    main()
