from pydantic import BaseModel


class ValidationError(BaseModel):
    """Represents a validation error with context."""

    error_type: str
    message: str
    service: str | None = None
    field: str | None = None
    suggestion: str | None = None

    def __str__(self) -> str:
        parts = []
        if self.service:
            parts.append(f"Service '{self.service}'")
        if self.field:
            parts.append(f"field '{self.field}'")

        prefix = " - ".join(parts) + ": " if parts else ""
        result = f"{prefix}{self.message}"

        if self.suggestion:
            result += f"\n  Suggestion: {self.suggestion}"

        return result
