"""LiteLLM gateway: key pool with automatic failover, response cache, failure-simulation hook."""
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

import litellm
from litellm import Cache

from .config import Settings

logger = logging.getLogger("llm_gateway")

SIMULATED_STATUSES = {429, 500, 502, 503}
LIVE_INFERENCE = "LIVE_INFERENCE"
LLM_CACHE = "LLM_CACHE"

litellm.suppress_debug_info = True
litellm.set_verbose = False


class AllProvidersUnavailable(Exception):
    """Raised when every key in the pool failed (real or simulated)."""

    def __init__(self, failures=None):
        super().__init__()
        self.failures = failures or []


class AllProvidersRateLimited(AllProvidersUnavailable):
    """Raised when every configured provider key is rate-limited."""


class ModelMessagesCache(Cache):
    """LiteLLM cache whose key depends only on `model` + the full message list.

    LiteLLM's default key also hashes `api_key` and other call parameters, so the same prompt answered
    by key_1 would miss when asked through key_2. Overriding `get_cache_key` removes that dependency.
    """

    def get_cache_key(self, *args, **kwargs) -> str:
        payload = json.dumps(
            {"model": kwargs.get("model"), "messages": kwargs.get("messages")},
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _provider_acompletion(**kwargs):
    """Single seam around LiteLLM so tests can mock the provider without touching the cache layer."""
    return await litellm.acompletion(**kwargs)


def parse_failure_header(value: Optional[str]) -> Dict[str, int]:
    """Parse `key_1=429,key_2=500` / `*=429` into {key_id: status}. Invalid pairs are ignored."""
    result: Dict[str, int] = {}
    if not value:
        return result
    for part in value.split(","):
        if "=" not in part:
            continue
        key, _, raw_status = part.partition("=")
        key, raw_status = key.strip(), raw_status.strip()
        if not key:
            continue
        try:
            status = int(raw_status)
        except ValueError:
            continue
        if status in SIMULATED_STATUSES:
            result[key] = status
    return result


@dataclass
class GatewayResult:
    reply: str
    model: str
    input_tokens: int
    output_tokens: int
    resolution_source: str
    key_id: Optional[str]
    fallback_attempts: int


class LLMGateway:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.model = settings.llm_model
        self.keys: List[str] = list(settings.llm_api_keys)
        self.cache = ModelMessagesCache(type="local")
        litellm.cache = self.cache

    @property
    def cache_enabled(self) -> bool:
        return litellm.cache is self.cache

    async def warmup(self) -> None:
        """Run one offline completion twice so LiteLLM's lazy cache imports are loaded before real traffic."""
        messages = [{"role": "user", "content": "__warmup__"}]
        try:
            for _ in range(2):
                await _provider_acompletion(
                    model=self.model,
                    messages=messages,
                    api_key="warmup",
                    mock_response="ok",
                    caching=True,
                    num_retries=0,
                )
        except Exception as exc:  # warmup is best effort only
            logger.warning("cache warmup skipped: %s", type(exc).__name__)

    async def complete(self, messages: List[dict], simulate: Optional[Dict[str, int]] = None) -> GatewayResult:
        simulate = simulate if (self.settings.enable_failure_simulation and simulate) else {}
        attempts = 0
        failures = []
        failure_details = []
        for index, api_key in enumerate(self.keys, start=1):
            key_id = f"key_{index}"
            simulated_status = simulate.get(key_id, simulate.get("*"))
            if simulated_status is not None:
                logger.warning("simulated failure key_id=%s status=%s", key_id, simulated_status)
                failure_details.append(f"{key_id}: simulated HTTP {simulated_status}")
                attempts += 1
                continue
            call_kwargs = dict(
                model=self.model,
                messages=messages,
                api_key=api_key,
                timeout=self.settings.request_timeout,
                num_retries=0,
                caching=True,
            )
            if simulate:
                # A simulated-failure request must exercise the live failover path, so skip the cache read.
                call_kwargs["cache"] = {"no-cache": True}
            try:
                response = await _provider_acompletion(**call_kwargs)
                reply = (response.choices[0].message.content or "").strip()
                if not reply:
                    raise ValueError("empty completion")
            except Exception as exc:
                # Log the exception type only: never the key, never a stack trace.
                logger.warning("provider failure key_id=%s error=%s", key_id, type(exc).__name__)
                failures.append(exc)
                failure_details.append(f"{key_id}: {type(exc).__name__}")
                attempts += 1
                continue

            hidden = getattr(response, "_hidden_params", None) or {}
            cache_hit = bool(hidden.get("cache_hit"))
            usage = getattr(response, "usage", None)
            return GatewayResult(
                reply=reply,
                model=self.model,
                input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
                resolution_source=LLM_CACHE if cache_hit else LIVE_INFERENCE,
                key_id=None if cache_hit else key_id,
                fallback_attempts=0 if cache_hit else attempts,
            )
        if failures and len(failures) == len(self.keys) and all(
            isinstance(exc, litellm.RateLimitError) for exc in failures
        ):
            raise AllProvidersRateLimited(failure_details)
        raise AllProvidersUnavailable(failure_details)
