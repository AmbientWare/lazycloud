from __future__ import annotations

from shared.http.gateway import StringList


def string_list_filters(filters: dict[str, list[str]] | None) -> dict[str, StringList]:
    return {key: StringList(values=list(values)) for key, values in (filters or {}).items()}
