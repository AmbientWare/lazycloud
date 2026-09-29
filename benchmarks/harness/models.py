from pydantic import BaseModel, ConfigDict


class BenchmarkModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
