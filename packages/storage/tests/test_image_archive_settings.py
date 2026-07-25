from __future__ import annotations

import pytest
from pydantic import ValidationError
from storage.image_archive import (
    ImageArchiveBackendSettings,
    ImageArchiveSettings,
)


def test_image_archive_rejects_ambiguous_bucket_and_backend() -> None:
    with pytest.raises(ValidationError, match="cannot be set with a separate backend"):
        ImageArchiveSettings(
            bucket="archives",
            backend=ImageArchiveBackendSettings(bucket="other-archives"),
        )
