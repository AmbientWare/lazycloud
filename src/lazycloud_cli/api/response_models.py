"""
Response models for all API operations.
"""

from typing import Optional
from datetime import datetime
from pydantic import BaseModel


# User API Response Models
class UserInfoResponse(BaseModel):
    user_id: str
    email: Optional[str] = None
    name: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # Add other user fields as needed


class UserIdResponse(BaseModel):
    user_id: str


# Generic success/error responses
class SuccessResponse(BaseModel):
    status: str = "success"
    message: str


class ErrorResponse(BaseModel):
    detail: str
    status_code: Optional[int] = None
