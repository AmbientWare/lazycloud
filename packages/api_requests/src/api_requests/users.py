from pydantic import BaseModel


class OnboardingRequest(BaseModel):
    workos_id: str
    name: str
    email: str
