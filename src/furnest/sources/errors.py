"""Errors a source raises when it cannot return listings.

The pipeline records both on the source's health row. :class:`SourceBlockedError`
is kept separate because a block (HTTP 403, a captcha page, a robots.txt
disallow) is not fixed by retrying sooner. It needs a slower schedule, a proxy, or
a person to look at it.
"""

from __future__ import annotations


class SourceError(Exception):
    """A request to a source failed: transport error, bad status, retries used up."""

    def __init__(self, message: str, *, url: str, status: int | None = None) -> None:
        super().__init__(message)
        self.url = url
        self.status = status


class SourceBlockedError(SourceError):
    """The source refused us: HTTP 403, a captcha or challenge page, or robots.txt."""

    def __init__(self, message: str, *, url: str, reason: str, status: int | None = None) -> None:
        super().__init__(message, url=url, status=status)
        self.reason = reason
