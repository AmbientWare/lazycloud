from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest
from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from provider_aws.managed_pool import (
    AwsManagedPoolBootstrap,
    AwsManagedPoolSpec,
    aws_managed_pool_bootstrap_script,
)

_AGENT_SHA256 = "a" * 64
_AGENT_BINARY_URL = (
    f"https://s3.us-east-1.amazonaws.com/releases/agents/0.1.0/{_AGENT_SHA256}/"
    "lazycloud-agent-linux-amd64"
)
_ACCESS_KEY = "ASIA0123456789ABCDEF"
_SECRET_KEY = "0123456789abcdefghijklmnopqrstuvwxyzABCD"
_SESSION_TOKEN = "temporary/session-token"


def _spec() -> AwsManagedPoolSpec:
    return AwsManagedPoolSpec(
        workspace_id="12345678-1234-4123-8123-123456789abc",
        pool_name="bootstrap",
        region="us-east-1",
        instance_type="i4i.xlarge",
        ami_id="ami-0123456789abcdef0",
        desired_nodes=0,
        max_nodes=1,
        root_volume_gib=50,
        node_instance_profile_arn="arn:aws:iam::123456789012:instance-profile/compute-node",
        vpc_id="vpc-0123456789abcdef0",
        subnet_ids=("subnet-0123456789abcdef0", "subnet-0123456789abcdef1"),
        security_group_id="sg-0123456789abcdef0",
        bootstrap=AwsManagedPoolBootstrap(
            control_plane_url="https://compute.example.com",
            enrollment_request_id="12345678-1234-4123-8123-123456789abc",
            agent_version="0.1.0",
            agent_sha256=_AGENT_SHA256,
            agent_binary_url=_AGENT_BINARY_URL,
            worker_image_digest=f"registry.example.com/worker@sha256:{'b' * 64}",
        ),
    )


def _require_tooling() -> tuple[str, str]:
    bash = shutil.which("bash")
    openssl = shutil.which("openssl")
    if bash is None or openssl is None:
        pytest.fail("bash and openssl are required to prove the generated bootstrap script")
    return bash, openssl


def test_bootstrap_script_reports_phases_and_bounded_failures_without_gateway_installs() -> None:
    bash, _ = _require_tooling()
    script = aws_managed_pool_bootstrap_script(_spec())

    syntax = subprocess.run(
        [bash, "-n", "/dev/stdin"], input=script, capture_output=True, text=True, check=False
    )
    assert syntax.returncode == 0, syntax.stderr

    # The gateway install route is never used: the agent binary comes from the
    # release artifact URL and is digest-verified before it runs.
    assert "/install/agent" not in script
    assert _AGENT_BINARY_URL in script
    assert 'curl -fsSL --retry 5 --retry-delay 2 "$AGENT_BINARY_URL" -o "$agent_download"' in (
        script
    )
    assert '[ "$(sha256sum "$agent_download" | awk \'{print $1}\')" != "$AGENT_SHA256" ]' in script

    # The booting report is the first act after identity material, before any
    # install step can touch the platform gateway.
    main_body = script[script.index("bootstrap_main() {") :]
    assert "report_phase booting" in main_body
    assert main_body.index("report_phase booting") < main_body.index("ensure_docker")
    assert main_body.index("ensure_docker") < main_body.index("report_phase joining")
    assert main_body.index("report_phase joining") < main_body.index("install-service")

    # The agent runs as a persistent service, never as a cloud-init child: a
    # cloud-init child dies with that script module, leaving a machine that
    # enrolls once and then has no agent, so its worker never leaves `pending`.
    #
    # The agent installs that unit itself. This script must not write one too:
    # when both did, a machine ran the agent's unit while this script claimed a
    # different restart policy, so a fix made here never reached any machine.
    # The unit's contents are owned and proven by render_systemd_unit.
    assert '"$AGENT_BIN" install-service' in script
    assert "/etc/systemd/system/lazycloud-agent.service" not in script
    assert 'exec "$AGENT_BIN" join' not in script

    # Every failure posts one bounded, exact enrollment failure reason.
    assert "docker) reason=runtime_install_failed ;;" in script
    assert "tailscale) reason=network_join_failed ;;" in script
    assert "agent) reason=agent_download_failed ;;" in script
    assert 'report_failure "$reason"' in script
    assert "report bootstrap-failure failure_reason" in script
    assert "report bootstrap-phase phase" in script
    # Each report mints a fresh single-use proof.
    assert 'proof="$(mint_proof)"' in script


def test_shell_minted_sigv4_proof_matches_botocore_query_auth(tmp_path: Path) -> None:
    bash, _ = _require_tooling()

    request = AWSRequest(
        method="GET",
        url="https://sts.us-east-1.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
    )
    SigV4QueryAuth(
        Credentials(access_key=_ACCESS_KEY, secret_key=_SECRET_KEY, token=_SESSION_TOKEN),
        "sts",
        "us-east-1",
        expires=30,
    ).add_auth(request)
    if request.url is None:
        pytest.fail("botocore SigV4 signer did not produce a URL")
    reference = dict(parse_qsl(urlsplit(request.url).query, keep_blank_values=True))

    script = aws_managed_pool_bootstrap_script(_spec())
    lines = script.rstrip("\n").split("\n")
    assert lines[-1] == "bootstrap_main"
    driver = tmp_path / "mint_proof.sh"
    driver.write_text(
        "\n".join(
            [
                *lines[:-1],
                "trap - ERR",
                'REGION="us-east-1"',
                'STS_HOST="sts.us-east-1.amazonaws.com"',
                f'AWS_ACCESS_KEY_ID="{_ACCESS_KEY}"',
                f'AWS_SECRET_ACCESS_KEY="{_SECRET_KEY}"',
                f"AWS_SESSION_TOKEN='{_SESSION_TOKEN}'",
                f'AMZ_DATE="{reference["X-Amz-Date"]}"',
                "mint_proof",
                "",
            ]
        ),
        encoding="utf-8",
    )
    minted = subprocess.run(
        [bash, driver.as_posix()], capture_output=True, text=True, check=True
    ).stdout.strip()

    parts = urlsplit(minted)
    assert parts.scheme == "https"
    assert parts.netloc == "sts.us-east-1.amazonaws.com"
    assert parts.path == "/"
    assert dict(parse_qsl(parts.query, keep_blank_values=True)) == reference
