from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass

from database.repositories.search import ResourceSearchRepository
from pydantic import BaseModel, ValidationError
from shared.errors import InvalidInputError
from shared.http.search import ResourceSearchResponse, ResourceSearchResult

from control.context import ControlContext


class SearchCursor(BaseModel):
    workspace_id: str
    query: str
    kind: str
    name: str
    id: str


@dataclass(slots=True)
class ResourceSearchService:
    context: ControlContext

    def search(
        self, workspace_id: str, query: str, *, cursor: str = "", limit: int = 30
    ) -> ResourceSearchResponse:
        query = query.strip()
        if not 1 <= limit <= 100 or not query or len(query) > 240:
            raise InvalidInputError("Search requires 1 to 240 characters and a limit of 1 to 100")
        after: tuple[str, str, str] | None = None
        if cursor:
            try:
                decoded = SearchCursor.model_validate_json(base64.urlsafe_b64decode(cursor))
            except (ValueError, binascii.Error, ValidationError) as exc:
                raise InvalidInputError("Invalid search cursor") from exc
            if decoded.workspace_id != workspace_id or decoded.query != query:
                raise InvalidInputError("Search cursor belongs to a different query")
            after = decoded.kind, decoded.name, decoded.id
        with self.context.database.session() as session:
            records = ResourceSearchRepository(session).search(
                workspace_id, query, after=after, limit=limit + 1
            )
        page = records[:limit]
        next_cursor = ""
        if len(records) > limit:
            last = page[-1]
            next_cursor = base64.urlsafe_b64encode(
                SearchCursor(
                    workspace_id=workspace_id,
                    query=query,
                    kind=last.kind,
                    name=last.name,
                    id=last.id,
                )
                .model_dump_json()
                .encode()
            ).decode()
        return ResourceSearchResponse(
            data=[ResourceSearchResult.model_validate(item.model_dump()) for item in page],
            next=next_cursor,
        )
