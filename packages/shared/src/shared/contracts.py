from pydantic import BaseModel, ConfigDict


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


__all__ = ["ContractModel"]
