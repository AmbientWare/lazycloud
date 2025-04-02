from fastapi import HTTPException, Request, FastAPI
from fastapi.security import HTTPBearer
from machines.database import db
from machines.config import app_config, ENVIRONMENT

bearer = HTTPBearer()


def setup_middleware(app: FastAPI):

    @app.middleware("http")
    async def authenticate_user(request: Request, call_next):
        if app_config.ENV.value == ENVIRONMENT.DEV.value:
            request.state.user_id = "dev-user"
            return await call_next(request)

        try:
            # This will handle the "Bearer " prefix and token extraction
            auth = await bearer(request)
            if not auth:
                raise HTTPException(status_code=401, detail="Unauthorized")

            # Get the token without the "Bearer " prefix
            token = auth.credentials

            # Use the async version of user_by_token
            user_id = await db.tokens.auser_by_token(token)
            if not user_id:
                raise HTTPException(status_code=401, detail="Invalid token")

            request.state.user_id = user_id
            return await call_next(request)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=401, detail="Invalid authentication credentials"
            )
