from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from shared.deployments import DeploymentKind

MAX_DNS_LABEL_LENGTH = 63
DNS_LABEL_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
LATEST_MARKER = "latest"

_DIGEST_LENGTH = 8
_VERSION_MARKER_PATTERN = re.compile(r"^v([1-9][0-9]*)$")
_NON_LABEL_RUN = re.compile(r"[^a-z0-9]+")

# The longest suffix a host can append to the subdomain. Budgeted here rather than
# where the label is built, so a long resource name is shortened once at mint time
# instead of overflowing the DNS limit only for whoever pins a version.
_MAX_SUFFIX_LENGTH = len(f"-{LATEST_MARKER}")
_MAX_STEM_LENGTH = MAX_DNS_LABEL_LENGTH - 1 - _DIGEST_LENGTH - _MAX_SUFFIX_LENGTH


@dataclass(frozen=True, slots=True)
class DeploymentHostTarget:
    subdomain: str
    version: int | None = None
    """The version the host pinned, or `None` for whichever is latest."""


def deployment_subdomain(
    *,
    workspace_id: str,
    app_name: str,
    name: str,
    kind: DeploymentKind,
) -> str:
    """Mint the DNS label that addresses one resource across all of its versions.

    Version is deliberately not an input: every version of a resource answers on this
    one label, and a host pins one by appending `-v3`. That keeps the label a caller
    publishes stable across redeploys while leaving an older version addressable.

    Keyed on the app's name rather than its id, because the id is not yet known on the
    deploy that creates the app, and because a name is what a user considers stable —
    an app deleted and recreated under the same name keeps the URL it published.

    Resource names are unique only within an app, so the label carries a digest of the
    full identity to make it unique across the whole platform — at the edge a request
    has no token and the hostname is the only routing key. The digest is hex, which
    cannot contain `v`, so it can never be misread as the `-v3` that may follow it.
    """

    stem = _NON_LABEL_RUN.sub("-", name.strip().lower()).strip("-")
    if len(stem) > _MAX_STEM_LENGTH:
        stem = stem[:_MAX_STEM_LENGTH].rstrip("-")
    # A separator no field can contain, so distinct identities cannot concatenate into
    # the same digest input.
    identity = "\x00".join((workspace_id, app_name, name, kind.value))
    digest = hashlib.sha256(identity.encode()).hexdigest()[:_DIGEST_LENGTH]
    return f"{stem}-{digest}" if stem else digest


def parse_deployment_host(label: str) -> DeploymentHostTarget | None:
    """Resolve a host label to the resource and version it addresses, or `None`.

    `<subdomain>` and `<subdomain>-latest` both address whichever version is latest;
    `<subdomain>-v3` pins one. Splitting the suffix off the right is unambiguous because
    the subdomain always ends in a hex digest, which can be neither `latest` nor `vN`.
    """

    normalized = label.strip().lower()
    if not DNS_LABEL_PATTERN.fullmatch(normalized):
        return None
    if "-" not in normalized:
        return DeploymentHostTarget(subdomain=normalized)
    subdomain, marker = normalized.rsplit("-", 1)
    if marker == LATEST_MARKER:
        return DeploymentHostTarget(subdomain=subdomain)
    version_match = _VERSION_MARKER_PATTERN.fullmatch(marker)
    if version_match is None:
        return DeploymentHostTarget(subdomain=normalized)
    return DeploymentHostTarget(subdomain=subdomain, version=int(version_match.group(1)))


def deployment_host_label(subdomain: str, *, version: int | None = None) -> str:
    """The label to publish for a resource, pinned to a version only when asked."""

    return subdomain if version is None else f"{subdomain}-v{version}"


__all__ = [
    "DNS_LABEL_PATTERN",
    "LATEST_MARKER",
    "MAX_DNS_LABEL_LENGTH",
    "DeploymentHostTarget",
    "deployment_host_label",
    "deployment_subdomain",
    "parse_deployment_host",
]
