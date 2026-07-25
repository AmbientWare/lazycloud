from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest
from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from provider_aws import (
    AwsProviderNodeIdentityError,
    AwsProviderNodeIdentityErrorCode,
    AwsProviderNodeIdentityTarget,
    AwsProviderNodeIdentityTransportError,
    AwsProviderNodeIdentityVerifier,
    AwsProviderNodeReplayGuardError,
    AwsStsGetCallerIdentityProof,
    AwsStsProofHttpResponse,
)
from pydantic import SecretStr, ValidationError

_ACCOUNT_ID = "123456789012"
_INSTANCE_ID = "i-0123456789abcdef0"
_REGION = "us-east-1"
_ROLE_NAME = "compute-node-role"
_NODE_ROLE_ARN = f"arn:aws:iam::{_ACCOUNT_ID}:role/customer-compute/{_ROLE_NAME}"
_PROFILE_ARN = f"arn:aws:iam::{_ACCOUNT_ID}:instance-profile/customer-compute/compute-node-profile"
_ASG_NAME = "cloud-pool-workspace-training"
_CALLER_ARN = f"arn:aws:sts::{_ACCOUNT_ID}:assumed-role/{_ROLE_NAME}/{_INSTANCE_ID}"
_CALLER_USER_ID = f"AROA0123456789ABCDEFG:{_INSTANCE_ID}"


def _target(**updates: str) -> AwsProviderNodeIdentityTarget:
    values = {
        "account_id": _ACCOUNT_ID,
        "region": _REGION,
        "node_role_arn": _NODE_ROLE_ARN,
        "node_instance_profile_arn": _PROFILE_ARN,
        "autoscaling_group_name": _ASG_NAME,
        **updates,
    }
    return AwsProviderNodeIdentityTarget.model_validate(values)


@lru_cache(maxsize=4)
def _presigned_url(region: str = _REGION) -> str:
    request = AWSRequest(
        method="GET",
        url=(f"https://sts.{region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15"),
    )
    signer = SigV4QueryAuth(
        Credentials(
            access_key="ASIA0123456789ABCDEF",
            secret_key="0123456789abcdefghijklmnopqrstuvwxyzABCD",
            token="temporary/session+token=0123456789",
        ),
        "sts",
        region,
        expires=30,
    )
    signer.add_auth(request)
    if request.url is None:
        raise AssertionError("SigV4 signer did not produce a request URL")
    return request.url


def _signed_at(url: str) -> datetime:
    query = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
    return datetime.strptime(query["X-Amz-Date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


def _proof(
    url: str | None = None,
    *,
    region: str = _REGION,
    instance_id: str = _INSTANCE_ID,
) -> AwsStsGetCallerIdentityProof:
    return AwsStsGetCallerIdentityProof(
        presigned_url=SecretStr(url or _presigned_url(region)),
        region=region,
        instance_id=instance_id,
    )


def _identity_xml(
    *,
    account_id: str = _ACCOUNT_ID,
    caller_arn: str = _CALLER_ARN,
    user_id: str = _CALLER_USER_ID,
) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">'
        "<GetCallerIdentityResult>"
        f"<Arn>{caller_arn}</Arn>"
        f"<UserId>{user_id}</UserId>"
        f"<Account>{account_id}</Account>"
        "</GetCallerIdentityResult>"
        "<ResponseMetadata><RequestId>01234567-89ab-cdef-0123-456789abcdef</RequestId>"
        "</ResponseMetadata>"
        "</GetCallerIdentityResponse>"
    ).encode()


def _replace_url(
    url: str,
    *,
    scheme: str | None = None,
    netloc: str | None = None,
    path: str | None = None,
    fragment: str | None = None,
) -> str:
    parsed = urlsplit(url)
    return urlunsplit(
        (
            scheme if scheme is not None else parsed.scheme,
            netloc if netloc is not None else parsed.netloc,
            path if path is not None else parsed.path,
            parsed.query,
            fragment if fragment is not None else parsed.fragment,
        )
    )


def _replace_query(url: str, key: str, value: str | None) -> str:
    parsed = urlsplit(url)
    query = [(name, item) for name, item in parse_qsl(parsed.query) if name != key]
    if value is not None:
        query.append((key, value))
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


class _FakeHttpClient:
    def __init__(self, response: AwsStsProofHttpResponse | None = None) -> None:
        self.response = response or AwsStsProofHttpResponse(
            status_code=200,
            body=_identity_xml(),
            content_type="application/xml; charset=utf-8",
        )
        self.error: AwsProviderNodeIdentityTransportError | None = None
        self.calls: list[dict[str, str | float | int | bool]] = []

    def execute_presigned_get(
        self,
        *,
        url: str,
        timeout_seconds: float,
        max_response_bytes: int,
        follow_redirects: bool,
    ) -> AwsStsProofHttpResponse:
        self.calls.append(
            {
                "url": url,
                "timeout_seconds": timeout_seconds,
                "max_response_bytes": max_response_bytes,
                "follow_redirects": follow_redirects,
            }
        )
        if self.error is not None:
            raise self.error
        return self.response


class _FakeReplayGuard:
    def __init__(self) -> None:
        self.claimed = True
        self.error: AwsProviderNodeReplayGuardError | None = None
        self.calls: list[tuple[str, datetime]] = []

    def claim_once(self, *, proof_sha256: str, expires_at: datetime) -> bool:
        self.calls.append((proof_sha256, expires_at))
        if self.error is not None:
            raise self.error
        return self.claimed


def _verifier(
    *,
    http: _FakeHttpClient | None = None,
    replay: _FakeReplayGuard | None = None,
) -> tuple[AwsProviderNodeIdentityVerifier, _FakeHttpClient, _FakeReplayGuard]:
    effective_http = http or _FakeHttpClient()
    effective_replay = replay or _FakeReplayGuard()
    return (
        AwsProviderNodeIdentityVerifier(
            http_client=effective_http,
            replay_guard=effective_replay,
        ),
        effective_http,
        effective_replay,
    )


def _verify(
    verifier: AwsProviderNodeIdentityVerifier,
    proof: AwsStsGetCallerIdentityProof | None = None,
    *,
    target: AwsProviderNodeIdentityTarget | None = None,
    provider_machine_ids: tuple[str, ...] = (_INSTANCE_ID,),
    now: datetime | None = None,
):
    effective_proof = proof or _proof()
    effective_now = now or _signed_at(effective_proof.presigned_url.get_secret_value())
    return verifier.verify(
        effective_proof,
        target=target or _target(),
        provider_machine_ids=provider_machine_ids,
        now=effective_now,
    )


def _assert_error(
    code: AwsProviderNodeIdentityErrorCode,
    operation: Callable[[], object],
    *,
    forbidden: str | None = None,
) -> AwsProviderNodeIdentityError:
    with pytest.raises(AwsProviderNodeIdentityError) as caught:
        operation()
    assert caught.value.code is code
    if forbidden is not None:
        assert forbidden not in str(caught.value)
        assert forbidden not in caught.value.detail
    return caught.value


_INVALID_URL_MUTATORS: tuple[tuple[str, Callable[[str], str]], ...] = (
    ("scheme", lambda url: _replace_url(url, scheme="http")),
    ("host", lambda url: _replace_url(url, netloc="sts.us-west-2.amazonaws.com")),
    ("path", lambda url: _replace_url(url, path="/identity")),
    ("fragment", lambda url: _replace_url(url, fragment="secret")),
    ("unknown-query", lambda url: f"{url}&Unexpected=value"),
    ("action", lambda url: _replace_query(url, "Action", "AssumeRole")),
    ("expiry", lambda url: _replace_query(url, "X-Amz-Expires", "61")),
    ("session-token", lambda url: _replace_query(url, "X-Amz-Security-Token", None)),
)


def test_identity_target_requires_connection_iam_scope() -> None:
    assert _target().node_role_arn == _NODE_ROLE_ARN

    with pytest.raises(ValidationError):
        _target(node_role_arn=f"arn:aws:iam::210987654321:role/{_ROLE_NAME}")
    with pytest.raises(ValidationError):
        _target(node_instance_profile_arn=f"arn:aws-us-gov:iam::{_ACCOUNT_ID}:instance-profile/x")


def test_identity_verifier_accepts_connection_and_managed_inventory() -> None:
    url = _presigned_url()
    proof = _proof(url)
    verifier, http, replay = _verifier()

    verified = _verify(verifier, proof)

    assert verified.account_id == _ACCOUNT_ID
    assert verified.partition == "aws"
    assert verified.instance_id == _INSTANCE_ID
    assert verified.caller_arn == _CALLER_ARN
    assert verified.node_role_arn == _NODE_ROLE_ARN
    assert verified.node_instance_profile_arn == _PROFILE_ARN
    assert verified.autoscaling_group_name == _ASG_NAME
    assert verified.proof_sha256 == hashlib.sha256(url.encode()).hexdigest()
    assert url not in repr(proof)
    assert url not in proof.model_dump_json()
    assert url not in verified.model_dump_json()
    assert http.calls == [
        {
            "url": url,
            "timeout_seconds": 3.0,
            "max_response_bytes": 32 * 1024,
            "follow_redirects": False,
        }
    ]
    assert replay.calls == [(verified.proof_sha256, verified.proof_expires_at)]


@pytest.mark.parametrize(
    "mutate",
    [pytest.param(mutate, id=name) for name, mutate in _INVALID_URL_MUTATORS],
)
def test_identity_verifier_rejects_noncanonical_or_unscoped_urls(
    mutate: Callable[[str], str],
) -> None:
    url = mutate(_presigned_url())
    verifier, http, replay = _verifier()

    _assert_error(
        AwsProviderNodeIdentityErrorCode.InvalidProof,
        lambda: _verify(verifier, _proof(url)),
        forbidden=url,
    )

    assert http.calls == []
    assert replay.calls == []


def test_identity_verifier_enforces_region_and_freshness_before_sts() -> None:
    verifier, http, replay = _verifier()
    west_proof = _proof(region="us-west-2")
    _assert_error(
        AwsProviderNodeIdentityErrorCode.IdentityMismatch,
        lambda: _verify(verifier, west_proof),
    )

    url = _presigned_url()
    _assert_error(
        AwsProviderNodeIdentityErrorCode.ExpiredProof,
        lambda: _verify(
            verifier,
            _proof(url),
            now=_signed_at(url) + timedelta(seconds=31),
        ),
        forbidden=url,
    )
    assert http.calls == []
    assert replay.calls == []


def test_identity_verifier_requires_exact_role_and_instance_session() -> None:
    wrong_role = "other-compute-role"
    role_http = _FakeHttpClient(
        AwsStsProofHttpResponse(
            status_code=200,
            body=_identity_xml(
                caller_arn=(f"arn:aws:sts::{_ACCOUNT_ID}:assumed-role/{wrong_role}/{_INSTANCE_ID}")
            ),
            content_type="application/xml",
        )
    )
    verifier, _, replay = _verifier(http=role_http)
    _assert_error(AwsProviderNodeIdentityErrorCode.IdentityMismatch, lambda: _verify(verifier))
    assert replay.calls == []

    other_instance = "i-0fedcba9876543210"
    session_http = _FakeHttpClient(
        AwsStsProofHttpResponse(
            status_code=200,
            body=_identity_xml(
                caller_arn=(
                    f"arn:aws:sts::{_ACCOUNT_ID}:assumed-role/{_ROLE_NAME}/{other_instance}"
                ),
                user_id=f"AROA0123456789ABCDEFG:{other_instance}",
            ),
            content_type="application/xml",
        )
    )
    verifier, _, replay = _verifier(http=session_http)
    _assert_error(AwsProviderNodeIdentityErrorCode.IdentityMismatch, lambda: _verify(verifier))
    assert replay.calls == []


def test_identity_verifier_requires_managed_asg_inventory_membership() -> None:
    verifier, _, replay = _verifier()

    _assert_error(
        AwsProviderNodeIdentityErrorCode.IdentityMismatch,
        lambda: _verify(verifier, provider_machine_ids=()),
    )
    _assert_error(
        AwsProviderNodeIdentityErrorCode.UpstreamUnavailable,
        lambda: _verify(verifier, provider_machine_ids=(_INSTANCE_ID, _INSTANCE_ID)),
    )
    _assert_error(
        AwsProviderNodeIdentityErrorCode.UpstreamUnavailable,
        lambda: _verify(verifier, provider_machine_ids=("invalid-provider-id",)),
    )
    assert replay.calls == []


def test_identity_verifier_requires_atomic_durable_replay_claim() -> None:
    replay = _FakeReplayGuard()
    replay.claimed = False
    verifier, http, _ = _verifier(replay=replay)

    _assert_error(AwsProviderNodeIdentityErrorCode.ReplayedProof, lambda: _verify(verifier))
    assert len(http.calls) == 1
    assert len(replay.calls) == 1

    unavailable = _FakeReplayGuard()
    unavailable.error = AwsProviderNodeReplayGuardError("redis unavailable")
    unavailable_verifier, _, _ = _verifier(replay=unavailable)
    _assert_error(
        AwsProviderNodeIdentityErrorCode.UpstreamUnavailable,
        lambda: _verify(unavailable_verifier),
    )


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (302, AwsProviderNodeIdentityErrorCode.InvalidProof),
        (429, AwsProviderNodeIdentityErrorCode.UpstreamUnavailable),
        (503, AwsProviderNodeIdentityErrorCode.UpstreamUnavailable),
    ],
)
def test_identity_verifier_classifies_sts_failures(
    status_code: int,
    expected: AwsProviderNodeIdentityErrorCode,
) -> None:
    http = _FakeHttpClient(AwsStsProofHttpResponse(status_code=status_code, body=b""))
    verifier, _, replay = _verifier(http=http)

    _assert_error(expected, lambda: _verify(verifier))
    assert replay.calls == []


def test_identity_verifier_rejects_malformed_xml_and_transport_failure() -> None:
    malformed = _FakeHttpClient(
        AwsStsProofHttpResponse(
            status_code=200,
            body=b'<!DOCTYPE value [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>',
            content_type="application/xml",
        )
    )
    verifier, _, replay = _verifier(http=malformed)
    _assert_error(AwsProviderNodeIdentityErrorCode.InvalidProof, lambda: _verify(verifier))
    assert replay.calls == []

    url = _presigned_url()
    unavailable = _FakeHttpClient()
    unavailable.error = AwsProviderNodeIdentityTransportError("socket failure")
    verifier, _, replay = _verifier(http=unavailable)
    _assert_error(
        AwsProviderNodeIdentityErrorCode.UpstreamUnavailable,
        lambda: _verify(verifier, _proof(url)),
        forbidden=url,
    )
    assert replay.calls == []
