"""Environment-driven configuration. Every setting comes from an environment variable."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant. Answer clearly and concisely."
DEFAULT_MODEL = "gemini/gemini-3.6-flash"
DEFAULT_DATABASE_URL = "sqlite:////data/app.db"
DEFAULT_ORIGINS = "http://localhost:3000,http://localhost:3001"

# Read the repository-level .env for local development; hosted environments can
# continue to provide settings through their platform environment variables.
load_dotenv(Path(__file__).resolve().parents[2] / ".env", encoding="utf-8-sig")


def _split_csv(value: str) -> tuple:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _as_bool(value: str, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    llm_api_keys: tuple = ()
    llm_model: str = DEFAULT_MODEL
    database_url: str = DEFAULT_DATABASE_URL
    allowed_origins: tuple = (DEFAULT_ORIGINS,)
    enable_failure_simulation: bool = False
    default_system_prompt: str = DEFAULT_SYSTEM_PROMPT
    request_timeout: float = 30.0

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        try:
            timeout = float(env.get("LLM_REQUEST_TIMEOUT", "30"))
        except ValueError:
            timeout = 30.0
        return cls(
            llm_api_keys=_split_csv(env.get("LLM_API_KEYS", "")),
            llm_model=env.get("LLM_MODEL", "").strip() or DEFAULT_MODEL,
            database_url=env.get("DATABASE_URL", "").strip() or DEFAULT_DATABASE_URL,
            allowed_origins=_split_csv(env.get("ALLOWED_ORIGINS", DEFAULT_ORIGINS)) or (DEFAULT_ORIGINS,),
            enable_failure_simulation=_as_bool(env.get("ENABLE_FAILURE_SIMULATION", ""), False),
            default_system_prompt=env.get("DEFAULT_SYSTEM_PROMPT", "").strip() or DEFAULT_SYSTEM_PROMPT,
            request_timeout=timeout,
        )
