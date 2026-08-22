"""Mint a short-lived GitHub token as the organisation's App.

The App rather than the workflow's own token, and rather than a deploy key. A
workflow token cannot push to a branch another workflow watches without granting
the workflow write access to its own repository; a deploy key is scoped to one
repository, so the second repository that mattered would need a second
credential and a second place to rotate it. The App is installed on the
organisation, so a new repository is reachable by existing.

The token this prints lives for an hour and is never written anywhere.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from base64 import urlsafe_b64encode

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

_API = "https://api.github.com"
_CLI_TIMEOUT_SECONDS = 60
# GitHub rejects anything longer than ten minutes, and refuses a token whose
# clock is ahead of its own, so this leans on the short side of both.
_ASSERTION_SECONDS = 540


class TokenError(RuntimeError):
    """The token could not be minted."""


def _segment(payload: dict[str, object]) -> bytes:
    return urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=")


def _assertion(app_id: str, private_key_pem: str) -> str:
    """A JWT signed as the App, which is the only thing it is used for."""
    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    # Narrowed rather than assumed: GitHub issues RSA keys, and PKCS1v15 below is
    # meaningless on anything else, so a key of another type has to say so here
    # instead of failing inside a signature GitHub then rejects as malformed.
    if not isinstance(key, rsa.RSAPrivateKey):
        raise TokenError(f"the App private key is {type(key).__name__}, not RSA")
    now = int(time.time())
    header = _segment({"alg": "RS256", "typ": "JWT"})
    # Backdated by a minute because GitHub rejects an `iat` in its own future,
    # and a runner's clock is not this process's to trust.
    body = _segment({"iat": now - 60, "exp": now + _ASSERTION_SECONDS, "iss": app_id})
    signing_input = header + b"." + body
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return (signing_input + b"." + urlsafe_b64encode(signature).rstrip(b"=")).decode()


def _read_secret(secret_id: str, aws_cli: str) -> str:
    result = subprocess.run(
        [
            aws_cli,
            "secretsmanager",
            "get-secret-value",
            "--secret-id",
            secret_id,
            "--query",
            "SecretString",
            "--output",
            "text",
        ],
        capture_output=True,
        text=True,
        timeout=_CLI_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        # The value is never echoed, but the reason it could not be read is.
        raise TokenError(f"cannot read {secret_id}: {result.stderr.strip()[:300]}")
    return result.stdout.strip()


def _installation_token(assertion: str, installation_id: str) -> str:
    request = urllib.request.Request(
        f"{_API}/app/installations/{installation_id}/access_tokens",
        method="POST",
        headers={
            "Authorization": f"Bearer {assertion}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_CLI_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        raise TokenError(
            f"GitHub refused the App assertion: {error.code} {error.reason}"
        ) from error
    except urllib.error.URLError as error:
        raise TokenError(f"could not reach GitHub: {error.reason}") from error
    token = payload.get("token", "")
    if not token:
        raise TokenError("GitHub returned no token")
    return str(token)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-id", required=True, help="GitHub App id.")
    parser.add_argument(
        "--installation-id", required=True, help="Installation on the organisation."
    )
    parser.add_argument(
        "--private-key-secret",
        required=True,
        help="Secrets Manager entry holding the App's PEM.",
    )
    parser.add_argument("--aws-cli", default="aws")
    args = parser.parse_args()

    try:
        pem = _read_secret(args.private_key_secret, args.aws_cli)
        print(_installation_token(_assertion(args.app_id, pem), args.installation_id))
    except TokenError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
