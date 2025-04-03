from fastapi import APIRouter, Depends, HTTPException, Body
from pydantic import BaseModel
from typing import Literal, List, Optional

from machines.api.security import get_current_active_user, UserData, require_admin
from machines.database import db
from machines.database.tokens import TokenPydantic, TokenRole, TokenExpiration
from machines.database.utils import (
    generate_token,
    generate_token_expires_at,
    token_is_expired,
)

tokens_router = APIRouter(prefix="/tokens", tags=["tokens"])


# NOTE: Tokens can only be created by admins.
#       This allows us to keep track of authenticated users without a users db table.
#       Users can fetch and update their own tokens.
#       Users can only have one token at a time for now.


@tokens_router.get("")
async def get_tokens(
    user_id: str | None = None,
    current_user: UserData = Depends(get_current_active_user),
) -> List[TokenPydantic]:
    # check if user is admin
    is_admin = current_user.role == TokenRole.ADMIN

    if is_admin:
        # admins can fetch tokens for any and all users
        if user_id:
            tokens = await db.tokens.afind(filters={"user_id": user_id})
        else:
            tokens = await db.tokens.afind(filters={})
    else:
        # users can only fetch their own tokens
        if user_id:
            if user_id != current_user.user_id:
                raise HTTPException(
                    status_code=403,
                    detail="Not enough permissions. Only admins can fetch tokens for other users.",
                )

        # this should only return one token -> [TokenPydantic]
        tokens = await db.tokens.afind(filters={"user_id": current_user.user_id})

    if len(tokens) == 0:
        raise HTTPException(status_code=404, detail="No tokens found")

    if not is_admin:
        # check if the user has any expired tokens
        for token in tokens:
            if token_is_expired(token.expires_at):
                raise HTTPException(
                    status_code=400,
                    detail="Token expired. Please refresh your token.",
                )

    return tokens


class CreateTokenRequest(BaseModel):
    user_id: str
    expires_at: TokenExpiration


@tokens_router.post("")
async def create_token(
    request: CreateTokenRequest,
    _=Depends(require_admin),
) -> TokenPydantic:
    # check if user already has a token
    token = await db.tokens.afind_one(filters={"user_id": request.user_id})
    if token is not None:
        raise HTTPException(status_code=400, detail="User already has a token")

    # create a new token that expires at the requested time
    token = TokenPydantic(
        user_id=request.user_id,
        token=generate_token(),
        expires_at=generate_token_expires_at(request.expires_at),
        role=TokenRole.USER,
    )
    new_token = await db.tokens.acreate(token)
    if new_token is None:
        raise HTTPException(status_code=404, detail="Unable to create token")

    return new_token


class UpdateTokenRequest(BaseModel):
    user_id: str
    expires_at: TokenExpiration


@tokens_router.put("/{token_id}")
async def update_token(
    token_id: int,
    request: UpdateTokenRequest,
) -> TokenPydantic:
    token = await db.tokens.afind_one(
        filters={"id": token_id, "user_id": request.user_id}
    )
    if token is None:
        raise HTTPException(status_code=404, detail="Token not found")

    # generate a new token
    token.token = generate_token()
    token.expires_at = generate_token_expires_at(request.expires_at)

    new_token = await db.tokens.aupdate(token)
    if new_token is None:
        raise HTTPException(status_code=404, detail="Unable to update token")

    return new_token


@tokens_router.delete("")
async def delete_tokens(
    token_id: Optional[int] = None,
    user_id: Optional[str] = None,
    _=Depends(require_admin),
) -> TokenPydantic:
    filters = {}
    if token_id:
        filters["id"] = token_id
    if user_id:
        filters["user_id"] = user_id

    token = await db.tokens.afind_one(filters=filters)
    if not token or token.id is None:
        raise HTTPException(status_code=404, detail="Token not found")

    await db.tokens.adelete(token.id)

    return token
