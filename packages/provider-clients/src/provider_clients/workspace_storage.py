from __future__ import annotations

from provider_aws.workspace_storage import (
    AwsWorkspaceStorageIssuer,
    AwsWorkspaceStorageSettings,
)
from shared.workspace_storage import WorkspaceStorageIssuer


def aws_workspace_storage_issuer() -> WorkspaceStorageIssuer:
    """The AWS issuer, reached the way every other provider adapter is.

    Composition imports this rather than `provider_aws` directly, so the app
    depends on the adapter layer and not on one provider's package.
    """
    return AwsWorkspaceStorageIssuer(settings=AwsWorkspaceStorageSettings())


__all__ = ["aws_workspace_storage_issuer"]
