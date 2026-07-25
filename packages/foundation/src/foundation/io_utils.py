from __future__ import annotations

from shared.contracts import ContractModel


class OutputMessage(ContractModel):
    msg: str = ""
    done: bool = False
    success: bool = False
    archiving: bool = False
    warning: bool = False
    image_id: str = ""
    python_version: str = ""
