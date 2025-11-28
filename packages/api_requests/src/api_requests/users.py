from pydantic import BaseModel


class OnboardingRequest(BaseModel):
    clerk_id: str
    name: str
    email: str
