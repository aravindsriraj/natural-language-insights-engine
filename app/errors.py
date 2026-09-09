"""Typed application errors. The API maps these to status codes; nothing else leaks."""
from __future__ import annotations


class AppError(Exception):
    code = "internal_error"
    status = 500

    def __init__(self, message: str, detail: str | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict:
        body: dict = {"code": self.code, "message": self.message}
        if self.detail:
            body["detail"] = self.detail
        return {"error": body}


class NotFound(AppError):
    code = "not_found"
    status = 404


class BadRequest(AppError):
    code = "bad_request"
    status = 400


class PayloadTooLarge(AppError):
    code = "payload_too_large"
    status = 413


class IngestError(AppError):
    code = "ingest_failed"
    status = 422


class UnsafeQuery(AppError):
    """Raised by the SQL guard. Surfaced to the agent as a tool result, never to the caller."""

    code = "unsafe_query"
    status = 400


class QueryTimeout(AppError):
    code = "query_timeout"
    status = 504


class LLMUnavailable(AppError):
    code = "llm_unavailable"
    status = 503
