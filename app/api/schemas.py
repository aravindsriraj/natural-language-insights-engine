"""Request and response models. Validation happens here so handlers stay thin."""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class QueryRequest(BaseModel):
    dataset_id: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=3, max_length=2000)
    thread_id: str | None = Field(None, max_length=128,
                                  description="Continue an existing conversation")
    use_cache: bool = True

    @field_validator("question")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("question must not be blank")
        return v


class ColumnRoleUpdate(BaseModel):
    role: str = Field(min_length=1, max_length=32)
    description: str | None = Field(None, max_length=500)


class JobAccepted(BaseModel):
    job_id: str
    status: str
    poll_url: str
    stream_url: str
