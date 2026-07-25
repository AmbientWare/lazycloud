from __future__ import annotations

import sys
from pathlib import Path

import pytest
from foundation.handler_loading import evict_user_code_modules, load_callable


def test_handler_loader_imports_module_and_nested_callable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_path = tmp_path / "user_handlers.py"
    module_path.write_text(
        """
class Service:
    def run(self, value):
        return {"value": value, "loaded": True}

service = Service()
""",
        encoding="utf-8",
    )
    monkeypatch.setattr("foundation.handler_loading.USER_CODE_DIR", tmp_path)
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])

    handler = load_callable("user_handlers:service.run")

    assert handler("payload") == {"value": "payload", "loaded": True}
    assert "user_handlers" in sys.modules
    evict_user_code_modules(tmp_path)
    assert "user_handlers" not in sys.modules


def test_handler_loader_imports_file_and_rejects_non_callable(tmp_path: Path) -> None:
    module_path = tmp_path / "file_handlers.py"
    module_path.write_text(
        "def handler(value):\n    return value + 1\n\nnot_handler = 3\n",
        encoding="utf-8",
    )

    handler = load_callable(f"{module_path}:handler")
    assert handler(4) == 5

    with pytest.raises(TypeError, match="handler is not callable"):
        load_callable(f"{module_path}:not_handler")
    with pytest.raises(ValueError, match="missing a function name"):
        load_callable(f"{module_path}:")

    evict_user_code_modules(tmp_path)
