from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from database.records.apps import StubRecord
from database.repositories.execution import PodExecutionRepository
from shared.deployments import StubKind
from shared.urls import pod_proxy_url
from sqlalchemy.orm import Session


def rewrite_persisted_sandbox_url_visibility(
    session: Session,
    *,
    stub: StubRecord,
    app_public: bool,
) -> None:
    if stub.kind is not StubKind.Sandbox:
        return
    repository = PodExecutionRepository(session).urls
    exposures = repository.list_for_stub(
        stub_id=stub.id,
        workspace_id=stub.workspace_id,
    )
    for exposure in exposures:
        parsed = urlsplit(exposure.url)
        origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        canonical_url = pod_proxy_url(
            origin,
            resource=StubKind.Sandbox,
            stub_id=stub.id,
            port=exposure.port,
            public=stub.public or app_public,
            container_id=exposure.container_id,
        )
        if canonical_url == exposure.url:
            continue
        repository.upsert(
            container_id=exposure.container_id,
            port=exposure.port,
            url=canonical_url,
        )


__all__ = ["rewrite_persisted_sandbox_url_visibility"]
