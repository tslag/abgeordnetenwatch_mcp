"""Uniform tool result envelope and error translation."""
from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from fastmcp.exceptions import ToolError

from .client import AWSession
from .errors import AWApiError, AWBudgetExceeded, AWNotFound, AWRateLimited, AWUserError

ROLL_CALL_NOTE = (
    "Only roll-call votes (namentliche Abstimmungen) are recorded. A missing vote is not evidence of a position."
)


def page_info(total: int, returned: int, offset: int = 0) -> dict:
    return {"total": total, "returned": returned, "offset": offset, "has_more": offset + returned < total}


def envelope(
    summary: str,
    data: Any,
    *,
    session: AWSession | None = None,
    page: dict | None = None,
    notes: list[str] | None = None,
    sources: list[str | None] | None = None,
) -> dict:
    """Standard result shape: {summary, data, page?, notes, sources, meta}."""
    out: dict[str, Any] = {"summary": summary, "data": data}
    if page is not None:
        out["page"] = page
    out["notes"] = [n for n in (notes or []) if n]
    out["sources"] = list(dict.fromkeys(s for s in (sources or []) if s))
    meta: dict[str, Any] = {"data_as_of": datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ")}
    if session is not None:
        meta.update(session.usage())
    out["meta"] = meta
    return out


def tool_errors(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Translate API-layer errors into actionable ToolError messages (isError results)."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except AWUserError as exc:
            raise ToolError(str(exc)) from None
        except AWNotFound as exc:
            raise ToolError(f"Not found: {exc}. Check the id (aw_find_politician, aw_search_polls and aw_lookup return valid ids).") from None
        except AWRateLimited as exc:
            raise ToolError(str(exc)) from None
        except AWBudgetExceeded as exc:
            raise ToolError(str(exc)) from None
        except AWApiError as exc:
            raise ToolError(f"Abgeordnetenwatch API error: {exc.message}") from None

    return wrapper
