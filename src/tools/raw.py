"""aw_query: bounded escape hatch for entities/filters the specific tools do not cover.

Registered only when AW_ENABLE_RAW_QUERY=1, to keep the default tool surface small.
"""
from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from aw.endpoints import ENDPOINTS, FILTER_OPERATORS, TTL_ENTITY
from aw.envelope import envelope, page_info, tool_errors
from aw.errors import AWUserError
from tools.common import READ_ONLY, new_session

Entity = Literal[tuple(sorted(ENDPOINTS))]  # type: ignore[valid-type]
_RESERVED = {"related_data", "range_start", "range_end", "page", "pager_limit", "sort_by", "sort_direction"}
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[A-Za-z0-9_.]+\])*$")


def validate_filters(filters: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in filters.items():
        if not _KEY.match(key):
            raise AWUserError(f"Invalid filter key '{key}'. Use 'field', 'field[op]' or 'ref[entity.field][op]'.")
        if key.split("[")[0] in _RESERVED:
            raise AWUserError(f"'{key}' is controlled by the tool (limit/offset/sort_by); do not put it in filters.")
        brackets = re.findall(r"\[([^\]]+)\]", key)
        if brackets and "." not in brackets[-1] and brackets[-1] not in FILTER_OPERATORS:
            raise AWUserError(f"Unknown operator '{brackets[-1]}'. Allowed: {', '.join(sorted(FILTER_OPERATORS))}.")
        clean[key] = value
    return clean


_REF_KEYS = {"id", "entity_type", "label", "api_url", "abgeordnetenwatch_url"}
_NOISE = ("api_url", "entity_type")


def _collapse(value: Any) -> Any:
    """Nested reference objects ({id, label, api_url, ...}) become {id, label}; other objects are cleaned recursively."""
    if isinstance(value, list):
        return [_collapse(x) for x in value]
    if isinstance(value, dict):
        if "id" in value and set(value) <= _REF_KEYS:
            return {"id": value["id"], "label": value.get("label")}
        return {k: _collapse(v) for k, v in value.items() if k not in _NOISE}
    return value


def compact(row: Any) -> Any:
    """Drop api_url/entity_type noise from a row and flatten its nested references."""
    if not isinstance(row, dict):
        return row
    return {k: _collapse(v) for k, v in row.items() if k not in _NOISE}


def register_raw_tools(mcp: FastMCP) -> None:
    @mcp.tool(name="aw_query", annotations=READ_ONLY)
    @tool_errors
    async def aw_query(
        entity: Annotated[Entity, Field(description="Entity to list.")],
        filters: Annotated[dict[str, str | int | float | bool | list[int]] | None, Field(description='API filters, e.g. {"label[cn]": "Klima"}, {"field_poll_date[gte]": "2026-01-01"}, {"party": 1}. Operators: eq ne gt gte lt lte sw cn ew in notin.')] = None,
        sort_by: Annotated[str | None, Field(description="Field to sort by.")] = None,
        sort_direction: Annotated[Literal["asc", "desc"], Field()] = "desc",
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict:
        """LAST RESORT: list any Abgeordnetenwatch entity with raw filters. Prefer the specific tools.

        Notes: numeric string keys (income_level, category) compare as text; votes cannot be filtered
        by poll fields; candidacy-mandates hide former mandates unless current_on=all.
        """
        s = new_session(2)
        params = {**validate_filters(dict(filters or {})), "range_start": offset, "range_end": limit}
        if sort_by:
            params["sort_by"] = sort_by
            params["sort_direction"] = sort_direction
        res = await s.list(entity, params, TTL_ENTITY)
        rows = [compact(r) for r in res.items]
        return envelope(f"{res.total} {entity} row(s) match", rows, session=s, page=page_info(res.total, len(rows), offset))
