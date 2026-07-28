from __future__ import annotations

import os

import lazycloud.config


def test_suite_does_not_inherit_developer_configuration() -> None:
    """The suite resolves its own configuration, never the developer's.

    `ClientSettings` reads `.env` by design so the public CLI works from a
    checkout without a login step. That same file holds a real endpoint and admin
    token, and without this the suite silently resolved them: a run then depended
    on the machine it ran on, and a real credential was loaded into every test
    process.
    """
    settings = lazycloud.config.settings()

    assert not settings.token
    assert settings.endpoint == "http://127.0.0.1:9000"
    inherited = [
        name
        for name in os.environ
        if name.startswith(("LAZYCLOUD_", "AWS_"))
        and not name.startswith("LAZYCLOUD_TEST_")
        and name not in _DECLARED
    ]
    assert inherited == []


def _declared_names() -> frozenset[str]:
    from conftest import TEST_ENVIRONMENT_FILE

    names = {"LAZYCLOUD_HOME"}
    for line in TEST_ENVIRONMENT_FILE.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if entry and not entry.startswith("#"):
            names.add(entry.split("=", 1)[0].strip())
    return frozenset(names)


_DECLARED = _declared_names()
