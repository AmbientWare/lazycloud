from __future__ import annotations

import pytest
from pydantic import ValidationError
from storage.retention_settings import ArtifactRetentionSettings


def test_retention_settings_reject_inverted_retry_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LAZYCLOUD_ARTIFACT_RETENTION_RETRY_INITIAL_SECONDS", "60")
    monkeypatch.setenv("LAZYCLOUD_ARTIFACT_RETENTION_RETRY_MAX_SECONDS", "30")

    with pytest.raises(ValidationError, match="retry max seconds cannot be less"):
        ArtifactRetentionSettings()
