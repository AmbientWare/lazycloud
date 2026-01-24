from enum import StrEnum

from pydantic import BaseModel, Field


class FeedbackType(StrEnum):
    BUG = "bug"
    FEATURE = "feature"
    OTHER = "other"


class FeedbackRequest(BaseModel):
    feedback_type: FeedbackType
    message: str = Field(..., min_length=10, max_length=5000)


class FeedbackResponse(BaseModel):
    success: bool
    message: str
