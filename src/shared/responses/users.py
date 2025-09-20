from pydantic import BaseModel


class OnboardingResponse(BaseModel):
    success: bool
