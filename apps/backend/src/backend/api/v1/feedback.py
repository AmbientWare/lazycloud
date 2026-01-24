from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from models.feedback import FeedbackRequest, FeedbackResponse

from backend.api.security import get_current_active_user
from backend.database.users import UserPydantic
from backend.rate_limit import limiter
from backend.services.email import email_service

feedback_router = APIRouter(prefix="/feedback", tags=["feedback"])


@feedback_router.post("")
@limiter.limit("5/minute")
async def submit_feedback(
    request_obj: Request,
    request: FeedbackRequest,
    current_user: UserPydantic = Depends(get_current_active_user),
) -> FeedbackResponse:
    """Submit user feedback (bug report, feature request, or other)."""
    try:
        email_service.send_feedback(
            user_email=current_user.email,
            user_name=current_user.name,
            feedback_type=request.feedback_type,
            message=request.message,
            source="cli",
        )
        return FeedbackResponse(success=True, message="Feedback submitted successfully")

    except Exception as e:
        logger.error(f"Failed to submit feedback: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit feedback")
