from __future__ import annotations

from pathlib import Path

from lazycloud.self_update import DISTRIBUTION, InstallerKind, detect_installation

HOME = Path("/home/dev")


def _detect(prefix: str, *, installer: str = "pip", **environ: str):
    return detect_installation(
        prefix=Path(prefix),
        executable=Path(prefix) / "bin" / "python",
        installer=installer,
        environ=environ,
        home=HOME,
    )


def test_uv_tool_installs_upgrade_through_uv_tool() -> None:
    installation = _detect("/home/dev/.local/share/uv/tools/lazycloud-client", installer="uv")

    assert installation.kind is InstallerKind.UV_TOOL
    assert installation.command == ("uv", "tool", "upgrade", DISTRIBUTION)


def test_pipx_installs_upgrade_through_pipx() -> None:
    installation = _detect("/opt/pipx/venvs/lazycloud-client", PIPX_HOME="/opt/pipx")

    assert installation.kind is InstallerKind.PIPX
    assert installation.command == ("pipx", "upgrade", DISTRIBUTION)


def test_uv_managed_environments_upgrade_with_uv_pip() -> None:
    installation = _detect("/home/dev/project/.venv", installer="uv")

    assert installation.kind is InstallerKind.UV
    assert installation.command[:5] == ("uv", "pip", "install", "--upgrade", "--python")
    assert installation.command[-1] == DISTRIBUTION


def test_pip_environments_upgrade_with_their_own_interpreter() -> None:
    installation = _detect("/home/dev/project/.venv")

    assert installation.kind is InstallerKind.PIP
    assert installation.command == (
        "/home/dev/project/.venv/bin/python",
        "-m",
        "pip",
        "install",
        "--upgrade",
        DISTRIBUTION,
    )


def test_release_is_newer_only_for_a_higher_release() -> None:
    from shared.client_version import release_is_newer

    assert release_is_newer("0.0.8", "0.0.7")
    assert not release_is_newer("0.0.7", "0.1.0")
    assert not release_is_newer("0.0.7", "0.0.7")
    assert not release_is_newer("0.0.8", "0.1.0.dev0")
