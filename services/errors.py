"""Typed failures, so a caller can tell a bad order from a broken system.

Every failure here used to be a bare ``Exception``, which left the poller unable to
answer the only question that matters at the top of the loop: retry this later, or
give up on it? A customer name that matches nothing will never succeed on a retry;
an RFC connection that timed out probably will.
"""

from __future__ import annotations


class OrderAgentError(Exception):
    """Base class for every failure this service raises deliberately."""

    #: Whether re-running the same input could plausibly succeed later.
    retryable = False


# --------------------------------------------------------------------------- input


class ExtractionError(OrderAgentError):
    """The language model did not return an order this service can act on."""


class ResolutionError(OrderAgentError):
    """A name in the order matched nothing in SAP well enough to use."""

    def __init__(self, message: str, *, term: str, best_match: str | None = None,
                 score: float | None = None):
        super().__init__(message)
        self.term = term
        self.best_match = best_match
        self.score = score


# --------------------------------------------------------------------------- SAP


class SapError(OrderAgentError):
    """SAP refused the call."""


class SapConnectionError(SapError):
    """The RFC connection could not be established or dropped mid-call."""

    retryable = True


class SalesOrderError(SapError):
    """BAPI_SALESORDER_CREATEFROMDAT2 returned an error and was rolled back."""

    def __init__(self, message: str, *, messages: list[dict] | None = None):
        super().__init__(message)
        #: The RETURN table, kept so the failure can be logged in full.
        self.messages = messages or []
