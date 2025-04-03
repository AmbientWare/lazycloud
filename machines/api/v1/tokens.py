from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List, Optional

from machines.api.security import require_admin
from machines.database import db
from machines.database.tokens import TokenPydantic, TokenRole, TokenExpirationMinutes
from machines.database.utils import (
    generate_token,
    generate_token_expires_at,
)

tokens_router = APIRouter(
    prefix="/tokens", tags=["tokens"], dependencies=[Depends(require_admin)]
)


# NOTE: Tokens access is limited to admins. This allows us to keep track of authenticated users without a users db table.


@tokens_router.get("")
async def get_tokens(
    user_id: str | None = None,
) -> List[TokenPydantic]:
    # check if user is admin
    if user_id:
        tokens = await db.tokens.afind(filters={"user_id": user_id})
    else:
        tokens = await db.tokens.afind(filters={})

    if len(tokens) == 0:
        raise HTTPException(status_code=404, detail="No tokens found")

    return tokens


class CreateTokenRequest(BaseModel):
    user_id: str
    expires_at: TokenExpirationMinutes


@tokens_router.post("")
async def create_token(
    request: CreateTokenRequest,
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
    expires_at: TokenExpirationMinutes


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
) -> List[TokenPydantic]:
    """
    If user_id is provided, delete all tokens for the user.
    If token_id is provided, delete the token with the given id.
    If both, filter by both id and user_id to ensure we only delete the correct token.
    """
    filters = {}
    if token_id:
        filters["id"] = token_id
    if user_id:
        filters["user_id"] = user_id

    tokens = await db.tokens.afind(filters=filters)
    if not tokens or len(tokens) == 0:
        raise HTTPException(status_code=404, detail="Token not found")

    for token in tokens:
        if token.id is not None:
            await db.tokens.adelete(token.id)

    return tokens
