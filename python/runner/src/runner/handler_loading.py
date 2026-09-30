from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Callable
from typing import Any

from shared.env import importing_user_code


def load_handler(reference: str) -> Callable[..., Any]:
    """Import `module:qualname` from the working directory.

    The working directory goes first on the path so user modules win over
    installed ones. The runner imports its own modules before this runs, so a
    user module cannot replace them.
    """

    module_name, separator, qualname = reference.partition(":")
    if not separator or not module_name or not qualname:
        raise ValueError(f"handler reference must be 'module:qualname', got {reference!r}")
    working_directory = os.getcwd()
    if working_directory not in sys.path:
        sys.path.insert(0, working_directory)
    importlib.invalidate_caches()
    with importing_user_code():
        target: Any = importlib.import_module(module_name)
    for part in qualname.split("."):
        target = getattr(target, part)
    if not callable(target):
        raise TypeError(f"handler {reference} is not callable")
    return target


__all__ = ["load_handler"]
