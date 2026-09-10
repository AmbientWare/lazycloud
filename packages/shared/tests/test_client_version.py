from __future__ import annotations

from shared.client_version import release_is_newer


def test_release_is_newer_only_for_a_higher_release() -> None:
    assert release_is_newer("0.0.8", "0.0.7")
    assert not release_is_newer("0.0.7", "0.1.0")
    assert not release_is_newer("0.0.7", "0.0.7")
    assert not release_is_newer("0.0.8", "0.1.0.dev0")
