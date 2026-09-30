from pydantic import Field

from shared.http.base import HttpModel


class ExecutionEntryEvidence(HttpModel):
    elapsed_since_entry_seconds: float = Field(ge=0, allow_inf_nan=False)
