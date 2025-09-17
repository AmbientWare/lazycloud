from datetime import datetime

from pydantic import BaseModel


# User API Response Models
class UserInfoResponse(BaseModel):
    user_id: str
    email: str | None = None
    name: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # Add other user fields as needed


class UserIdResponse(BaseModel):
    user_id: str


# Generic success/error responses
class SuccessResponse(BaseModel):
    status: str = "success"
    message: str


class ErrorResponse(BaseModel):
    detail: str
    status_code: int | None = None
