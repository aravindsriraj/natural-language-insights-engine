"""Runtime configuration. Everything is env-driven; nothing dataset-specific lives here."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    data_dir: Path = ROOT / "data"

    llm_model: str = "google_genai:gemini-3.8-flash"
    # Thinking models spend output tokens before answering. Lower efforts cut both the
    # thinking and, because the agent explores less, the number of turns. Verified against
    # the evaluation suite rather than assumed; see README.
    llm_reasoning_effort: str = "medium"
    llm_fallback_model: str = "google_genai:gemini-2.5-flash"
    gemini_api_key: str = ""

    max_upload_mb: int = 512
    max_result_rows: int = 1000
    query_timeout_s: int = 30
    job_concurrency: int = 4
    agent_max_sql_calls: int = 6
    agent_max_model_calls: int = 16
    # Every other limit is a count. These two are the only clocks, and without them a
    # hung provider request has nothing to stop it.
    llm_timeout_s: int = 90
    question_timeout_s: int = 420

    @property
    def datasets_dir(self) -> Path:
        return self.data_dir / "datasets"

    @property
    def jobs_db(self) -> Path:
        return self.data_dir / "jobs.sqlite"

    @property
    def checkpoints_db(self) -> Path:
        return self.data_dir / "checkpoints.sqlite"

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache
def settings() -> Settings:
    s = Settings()
    # langchain-google-genai reads GOOGLE_API_KEY; the documented user-facing name is GEMINI_API_KEY.
    key = s.gemini_api_key or os.environ.get("GOOGLE_API_KEY", "")
    if key:
        os.environ.setdefault("GOOGLE_API_KEY", key)
    s.datasets_dir.mkdir(parents=True, exist_ok=True)
    return s


def llm_configured() -> bool:
    return bool(os.environ.get("GOOGLE_API_KEY") or settings().gemini_api_key)
