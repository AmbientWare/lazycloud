from fastapi import Request, HTTPException


def get_user_id(request: Request) -> str:
    try:
        return request.state.user_id

    except AttributeError:
        raise HTTPException(status_code=401, detail="Unauthorized")
