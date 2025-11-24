from typing import Optional

from pydantic import BaseModel


class UploadIntentRequest(BaseModel):
    """Request for upload intent (push credentials)."""

    deployment_name: str
    repo_name: str
    session_name: Optional[str] = None


class ImageExistsRequest(BaseModel):
    """Request to check if images exist."""

    deployment_name: str
    image_names: list[str]
