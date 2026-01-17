from enum import StrEnum


class StorageType(StrEnum):
    """Storage class types for user-facing display."""

    STANDARD = "Standard"
    SHARED = "Shared"


# Kubernetes storage class names
STORAGE_CLASS_EBS = "ebs-gp3"
STORAGE_CLASS_EFS = "efs-sc"

# Mapping from k8s storage class to user-facing type
STORAGE_CLASS_TO_TYPE: dict[str, StorageType] = {
    STORAGE_CLASS_EBS: StorageType.STANDARD,
    STORAGE_CLASS_EFS: StorageType.SHARED,
}
