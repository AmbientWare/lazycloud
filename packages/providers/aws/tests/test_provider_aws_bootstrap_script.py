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
from pydantic import SecretStr

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
            tailnet_auth_key=SecretStr("tskey-auth-0123456789abcdef"),
        ),
    )


def _require_tooling() -> tuple[str, str]:
    bash = shutil.which("bash")
    openssl = shutil.which("openssl")
    if bash is None or openssl is None:
        pytest.fail("bash and openssl are required to prove the generated bootstrap script")
    return bash, openssl


def test_bootstrap_script_refuses_an_agent_binary_that_fails_digest_verification(
    tmp_path: Path,
) -> None:
    bash, _ = _require_tooling()
    script = aws_managed_pool_bootstrap_script(_spec())

    syntax = subprocess.run(
        [bash, "-n", "/dev/stdin"], input=script, capture_output=True, text=True, check=False
    )
    assert syntax.returncode == 0, syntax.stderr

    # The gateway install route is never used, and the agent owns its own unit:
    # when this script wrote one too, a machine ran the agent's unit while this
    # script claimed a different restart policy, so a fix made here never
    # reached any machine.
    assert "/install/agent" not in script
    assert "/etc/systemd/system/lazycloud-agent.service" not in script

    agent_bin = tmp_path / "lazycloud-agent"
    curl_log = tmp_path / "curl.log"
    lines = script.rstrip("\n").split("\n")
    assert lines[-1] == "bootstrap_main"
    driver = tmp_path / "ensure_agent.sh"
    driver.write_text(
        "\n".join(
            [
                *lines[:-1],
                f'AGENT_BIN="{agent_bin}"',
                f'CURL_LOG="{curl_log}"',
                'mint_proof() { printf "https://sts.test/proof"; }',
                # Stand in for the network: record every request and serve an
                # agent artifact whose digest does not match AGENT_SHA256.
                "curl() {",
                '  local out="" data="" previous=""',
                '  printf "%s\\n" "$*" >>"$CURL_LOG"',
                '  for argument in "$@"; do',
                '    case "$previous" in',
                '      -o) out="$argument" ;;',
                '      --data) data="$argument" ;;',
                "    esac",
                '    previous="$argument"',
                "  done",
                '  if [ -n "$out" ]; then',
                '    printf "tampered-agent-binary" >"$out"',
                "    return 0",
                "  fi",
                '  printf "%s\\n" "$data" >>"$CURL_LOG"',
                "  return 0",
                "}",
                "STEP=agent",
                "ensure_agent",
                "",
            ]
        ),
        encoding="utf-8",
    )

    run = subprocess.run([bash, driver.as_posix()], capture_output=True, text=True, check=False)
    reported = curl_log.read_text(encoding="utf-8")

    assert run.returncode != 0
    assert not agent_bin.exists()
    assert not list(tmp_path.glob("lazycloud-agent.download.*"))
    # The artifact came from the pinned release URL, and the mismatch is
    # reported as one bounded enrollment failure reason. The whole payload is
    # pinned because the provider half of it is now a shell function the generic
    # script splices in: dropping a field there would leave every bootstrap
    # report rejected, and the report is deliberately best-effort, so nothing
    # else would say so.
    assert _AGENT_BINARY_URL in reported
    assert (
        '{"enrollment_request_id":"12345678-1234-4123-8123-123456789abc"'
        ',"provider":"aws","region":"","provider_instance_id":""'
        ',"identity_proof_url":"https://sts.test/proof"'
        ',"failure_reason":"agent_download_failed"}'
    ) in reported


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
