from pydantic import BaseModel


class SecretCollection(BaseModel):
    added: dict[str, str] | None = None
    removed: list[str] | None = None
