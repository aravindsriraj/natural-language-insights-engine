"""Model construction. Provider-agnostic: the model string comes from the environment."""
from __future__ import annotations

from functools import lru_cache

from langchain.chat_models import init_chat_model

from app.config import llm_configured, settings
from app.errors import LLMUnavailable


@lru_cache
def get_model(model: str | None = None):
    if not llm_configured():
        raise LLMUnavailable(
            "No model API key configured. Set GEMINI_API_KEY in your environment or .env."
        )
    return init_chat_model(model or settings().llm_model, temperature=0)


def fallback_models() -> list[str]:
    s = settings()
    out = [s.llm_model]
    if s.llm_fallback_model and s.llm_fallback_model != s.llm_model:
        out.append(s.llm_fallback_model)
    return out
