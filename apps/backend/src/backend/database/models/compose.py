import uuid
from datetime import datetime
from typing import Any

from cryptography.fernet import InvalidToken
from models.deployments import DeploymentStates
from models.helm import HelmValues
from pydantic import BaseModel, field_serializer, field_validator

from backend.database.models.base import (
    BaseDbModel,
    UUIDStr,
)
from backend.database.utils import (
    decrypt_dict,
    decrypt_string,
    encrypt_dict,
    encrypt_string,
)


class ComposeDeployment(BaseModel):
    """Pydantic model for a compose deployment with automatic encryption/decryption."""

    name: str
    workspace_id: UUIDStr
    namespace: str
    compose_yaml: str
    pending_compose_yaml: str | None = None
    helm_values: HelmValues | None = None
    current_helm_revision: int | None = None
    state: DeploymentStates = DeploymentStates.PENDING
    status_message: str | None = None
    deployed_at: datetime | None = None
    deleted_at: datetime | None = None
    current_task_run_id: UUIDStr | None = None
    depot_project_id: str | None = None
    # cluster_id is required - must be set explicitly by API based on placement logic
    cluster_id: str

    @field_validator("compose_yaml", "pending_compose_yaml", mode="before")
    @classmethod
    def decrypt_compose_fields(cls, value: Any) -> str | None:
        """Automatically decrypt compose fields when loading from database."""
        if value is None:
            return None
        if isinstance(value, str):
            try:
                return decrypt_string(value)
            except InvalidToken:
                # Not encrypted (migration from old data or new deployment)
                return value
            except Exception as e:
                # Real decryption error (corrupt data, wrong key, etc.)
                raise ValueError(f"Failed to decrypt compose field: {e}") from e
        return value

    @field_serializer("compose_yaml", "pending_compose_yaml", when_used="always")
    def serialize_compose_fields(self, value: str | None) -> str | None:
        """Automatically encrypt compose fields when dumping for database storage."""
        if value is None:
            return None
        try:
            return encrypt_string(value)
        except Exception as e:
            raise ValueError(f"Failed to encrypt compose field: {e}") from e

    @field_serializer("current_task_run_id", when_used="always")
    def serialize_task_run_id(self, value: uuid.UUID | str | None) -> str | None:
        """Ensure task_run_id is serialized as string."""
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return str(value)
        return value

    @field_validator("helm_values", mode="before")
    @classmethod
    def decrypt_helm_values(cls, value: Any) -> HelmValues | None:
        """Automatically decrypt helm_values when loading from database."""
        if value is None:
            return None

        if isinstance(value, str):
            try:
                decrypted_dict = decrypt_dict(value)
                return HelmValues.model_validate(decrypted_dict)

            except InvalidToken:
                # Not encrypted - shouldn't happen in normal flow
                raise ValueError(
                    "helm_values is not encrypted - possible data corruption"
                )

            except Exception as e:
                raise ValueError(f"Failed to decrypt helm_values: {e}") from e

        # Already a HelmValues object or dict
        return value

    @field_serializer("helm_values", when_used="always")
    def serialize_helm_values(self, value: HelmValues | None) -> str | None:
        """Automatically encrypt helm_values when dumping for database storage."""
        if value is None:
            return None

        if isinstance(value, HelmValues):
            # Encrypt it
            try:
                helm_dict = value.model_dump(by_alias=True)
                return encrypt_dict(helm_dict)

            except Exception as e:
                raise ValueError(f"Failed to encrypt helm_values: {e}") from e


class ComposeDeploymentInDb(ComposeDeployment, BaseDbModel):
    """Pydantic model for a compose deployment that is stored in the database"""

    ...
