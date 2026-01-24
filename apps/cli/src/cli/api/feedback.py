from models.feedback import FeedbackRequest, FeedbackResponse, FeedbackType

from cli.api.base import BaseAPI


class FeedbackAPI(BaseAPI):
    """API client for feedback endpoints"""

    def __init__(self):
        super().__init__("feedback")

    def submit_feedback(self, feedback_type: str, message: str) -> FeedbackResponse:
        """Submit feedback to the backend"""
        request = FeedbackRequest(
            feedback_type=FeedbackType(feedback_type),
            message=message,
        )
        response = self._post(json=request.model_dump())
        return FeedbackResponse(**response)
