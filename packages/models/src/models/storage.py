from enum import StrEnum


class StorageType(StrEnum):
    """Storage class types for user-facing display."""

    STANDARD = "Standard"
    SHARED = "Shared"


# Kubernetes storage class names (provider-agnostic)
STORAGE_CLASS_STANDARD = "juicefs-standard"
STORAGE_CLASS_SHARED = "juicefs-shared"

# Mapping from k8s storage class to user-facing type
STORAGE_CLASS_TO_TYPE: dict[str, StorageType] = {
    STORAGE_CLASS_STANDARD: StorageType.STANDARD,
    STORAGE_CLASS_SHARED: StorageType.SHARED,
}
