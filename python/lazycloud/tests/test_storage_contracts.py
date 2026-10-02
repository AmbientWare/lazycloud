from __future__ import annotations

from lazycloud._shared.secrets import SecretRecord


def test_secret_record_masks_values_and_excludes_them_from_representations() -> None:
    secret = SecretRecord(name="PRIVATE_TOKEN", value="plaintext-must-not-appear")

    assert secret.masked() == "********"
    assert "plaintext-must-not-appear" not in repr(secret)
    assert "plaintext-must-not-appear" not in str(secret)
    assert SecretRecord(name="EMPTY", value="").masked() == ""
