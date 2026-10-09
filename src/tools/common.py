"""Shared helpers for tool modules."""
from __future__ import annotations

from aw.client import AWSession, get_client

# Every tool only reads from a public API: safe to retry, never destructive, talks to the open web.
READ_ONLY = {
    "readOnlyHint": True,
    "idempotentHint": True,
    "destructiveHint": False,
    "openWorldHint": True,
}


def new_session(budget: int = 8) -> AWSession:
    return get_client().session(budget)
