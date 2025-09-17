from pydantic import BaseModel


class CLIVersionResponse(BaseModel):
    version: str
