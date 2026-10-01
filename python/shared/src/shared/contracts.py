from pydantic import BaseModel, ConfigDict


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class APIModel(BaseModel):
    """Base of the models generated from contracts/openapi.yaml.

    Each model builds its validator on first use rather than at import, so
    importing the SDK, as every user module and container start does, skips
    the hundreds of models a program never touches.
    """

    model_config = ConfigDict(defer_build=True)


__all__ = ["APIModel", "ContractModel"]
