from pydantic import BaseModel


class OnboardingResponse(BaseModel):
    success: bool


class CurrentUserResponse(BaseModel):
    id: str
