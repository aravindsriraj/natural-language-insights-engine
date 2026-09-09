"""Structured response contract. The API returns this shape; the UI renders it."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ChartSpec(BaseModel):
    type: Literal["bar", "line", "none"] = "none"
    x: str | None = Field(None, description="Column from the result to plot on the x axis")
    y: str | None = Field(None, description="Numeric column from the result to plot")
    title: str | None = None


class Answer(BaseModel):
    """The agent's final response."""

    answer: str = Field(description="The answer in markdown, leading with the finding")
    refused: bool = Field(False, description="True when the dataset cannot answer this question")
    refusal_reason: str | None = Field(None, description="Why, when refused is true")
    missing_concepts: list[str] = Field(
        default_factory=list, description="Columns or concepts the dataset would need"
    )
    assumptions: list[str] = Field(
        default_factory=list, description="Judgment calls a reader could disagree with"
    )
    clarification: str | None = Field(
        None, description="A question back to the user when the request was ambiguous"
    )
    chart: ChartSpec | None = None
    confidence: Literal["high", "medium", "low"] = "medium"
