from typing import Optional

from pydantic import BaseModel


class UploadIntentRequest(BaseModel):
    """Request for upload intent (push credentials)."""

    deployment_name: str
    repo_name: str
    session_name: Optional[str] = None
