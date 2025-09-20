from pydantic import BaseModel


class OnboardingRequest(BaseModel):
    user_id: str
