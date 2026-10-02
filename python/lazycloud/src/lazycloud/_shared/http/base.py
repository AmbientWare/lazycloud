from __future__ import annotations

from pydantic import ConfigDict

from lazycloud.contracts import ContractModel


class HttpModel(ContractModel):
    model_config = ConfigDict(
        extra="forbid",
        from_attributes=True,
        validate_assignment=True,
    )


__all__ = ["HttpModel"]
