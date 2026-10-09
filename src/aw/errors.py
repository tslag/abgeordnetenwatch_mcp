"""Errors raised by the API layer. Tools translate them into actionable ToolError messages."""
from __future__ import annotations


class AWError(Exception):
    """Base class for all API-layer errors."""


class AWUserError(AWError):
    """The request cannot be answered as asked; the message tells the caller what to change."""


class AWApiError(AWError):
    """The API answered with status=error or an unexpected HTTP status."""

    def __init__(self, message: str, http_status: int | None = None):
        super().__init__(message)
        self.message = message
        self.http_status = http_status


class AWNotFound(AWError):
    """A single entity (e.g. politicians/123) does not exist."""


class AWRateLimited(AWError):
    """Still HTTP 429 after the retry; the caller should wait about a minute."""


class AWBudgetExceeded(AWError):
    """A single tool call would exceed its request budget (protects the 30 req/min limit)."""
