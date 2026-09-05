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
from shared.compute_policy import UnitName

_AGENT_SHA256 = "a" * 64
_AGENT_BINARY_URL = (
    f"https://s3.us-east-1.amazonaws.com/releases/agents/0.1.0/{_AGENT_SHA256}/"
    "lazycloud-agent-linux-amd64"
)
_NONCE = "0123456789abcdef0123456789abcdef"
_ACCESS_KEY = "ASIA0123456789ABCDEF"
_SECRET_KEY = "0123456789abcdefghijklmnopqrstuvwxyzABCD"
_SESSION_TOKEN = "temporary/session-token"


def _spec() -> AwsManagedPoolSpec:
    return AwsManagedPoolSpec(
        workspace_id="12345678-1234-4123-8123-123456789abc",
        unit_name=UnitName("bootstrap"),
        region="us-east-1",
        instance_type="m7i.xlarge",
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
        ),
    )


def _require_tooling() -> tuple[str, str]:
    bash = shutil.which("bash")
    openssl = shutil.which("openssl")
    if bash is None or openssl is None:
        pytest.fail("bash and openssl are required to prove the generated bootstrap script")
    return bash, openssl


def test_shell_minted_sigv4_proof_matches_botocore_query_auth(tmp_path: Path) -> None:
    bash, _ = _require_tooling()

    request = AWSRequest(
        method="GET",
        url=(
            "https://sts.us-east-1.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15"
            f"&X-Lazycloud-Nonce={_NONCE}"
        ),
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
                f'PROOF_NONCE="{_NONCE}"',
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
