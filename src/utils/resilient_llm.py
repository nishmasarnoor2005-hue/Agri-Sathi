# src/utils/resilient_llm.py
"""
Resilient Auto-Switching LLM Engine for AgriSathi
--------------------------------------------------
Provides zero-downtime, continuous voice conversations by automatically
switching across multiple API keys, models, and providers (Google Gemini & Groq).

Prevents presentation/demo failure from:
- 429 Quota Exceeded / Rate Limits
- Transient 503 / 500 API outages
- Model deprecations or regional latency spikes
"""

import asyncio
import logging
import os
import time
from typing import Any, List, Optional, Dict

from livekit.agents import llm
from livekit.agents.types import NOT_GIVEN, DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions
from livekit.plugins import google, openai as lk_openai
from src.config import Config

logger = logging.getLogger("agrisathi.resilient_llm")


class LLMCandidate:
    """Represents a single LLM provider + key + model configuration with circuit-breaker state."""

    def __init__(
        self,
        name: str,
        provider: str,
        model: str,
        client: llm.LLM,
        api_key_masked: str = "",
        priority: int = 1,
    ):
        self.name = name
        self.provider = provider
        self.model = model
        self.client = client
        self.api_key_masked = api_key_masked
        self.priority = priority
        self.cooldown_until: float = 0.0
        self.total_calls: int = 0
        self.successful_calls: int = 0
        self.failed_calls: int = 0
        self.last_error: Optional[str] = None
        self.last_used_at: float = 0.0

    @property
    def is_available(self) -> bool:
        return time.time() >= self.cooldown_until

    @property
    def status(self) -> str:
        if not self.is_available:
            remaining = int(self.cooldown_until - time.time())
            return f"COOLDOWN ({remaining}s)"
        if self.successful_calls > 0:
            return "HEALTHY"
        return "READY"

    def mark_success(self):
        self.total_calls += 1
        self.successful_calls += 1
        self.cooldown_until = 0.0
        self.last_used_at = time.time()

    def mark_failure(self, exc: Exception):
        self.total_calls += 1
        self.failed_calls += 1
        err_msg = str(exc)
        self.last_error = err_msg
        self.last_used_at = time.time()

        err_lower = err_msg.lower()
        if "429" in err_lower or "quota" in err_lower or "exhausted" in err_lower or "rate" in err_lower:
            # Rate limit or quota hit: 90 seconds cooldown
            self.cooldown_until = time.time() + 90.0
            logger.warning(f"[CircuitBreaker] {self.name} hit rate-limit/quota. Cooldown for 90s.")
        else:
            # Transient error: 20 seconds cooldown
            self.cooldown_until = time.time() + 20.0
            logger.warning(f"[CircuitBreaker] {self.name} encountered error: {err_msg[:120]}. Cooldown for 20s.")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider,
            "model": self.model,
            "key_masked": self.api_key_masked,
            "status": self.status,
            "total_calls": self.total_calls,
            "successful_calls": self.successful_calls,
            "failed_calls": self.failed_calls,
            "last_error": self.last_error,
            "is_available": self.is_available,
        }


class ResilientLLMStream(llm.LLMStream):
    """
    Wraps an active LLM generation stream.
    If the primary provider fails before streaming tokens to the voice synthesizer,
    this seamlessly fails over to the next candidate in the pool without dropping the call.
    """

    def __init__(
        self,
        parent_llm: "ResilientAutoSwitchLLM",
        *,
        chat_ctx: llm.ChatContext,
        tools: Optional[List[Any]] = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        tool_choice: Any = NOT_GIVEN,
        extra_kwargs: Any = NOT_GIVEN,
        parallel_tool_calls: Any = NOT_GIVEN,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            parent_llm,
            chat_ctx=chat_ctx,
            tools=tools or [],
            conn_options=conn_options,
        )
        self._parent = parent_llm
        self._tool_choice = tool_choice
        self._extra_kwargs = extra_kwargs
        self._parallel_tool_calls = parallel_tool_calls

    async def _run(self) -> None:
        candidates = self._parent.get_ordered_candidates()
        last_error = None

        for candidate in candidates:
            if not candidate.is_available:
                continue

            child_stream = None
            chunks_emitted = 0

            logger.info(
                f"[AutoSwitch] Routing turn to '{candidate.name}' ({candidate.provider}/{candidate.model})"
            )

            try:
                kwargs = {
                    "chat_ctx": self._chat_ctx,
                    "tools": self._tools,
                    "conn_options": self._conn_options,
                }
                if self._tool_choice is not NOT_GIVEN:
                    kwargs["tool_choice"] = self._tool_choice
                if self._extra_kwargs is not NOT_GIVEN:
                    kwargs["extra_kwargs"] = self._extra_kwargs
                if self._parallel_tool_calls is not NOT_GIVEN:
                    kwargs["parallel_tool_calls"] = self._parallel_tool_calls

                child_stream = candidate.client.chat(**kwargs)

                async for chunk in child_stream:
                    chunks_emitted += 1
                    await self._event_ch.send(chunk)

                # Successful stream completion
                candidate.mark_success()
                self._parent.record_turn_success(candidate)
                return

            except Exception as exc:
                last_error = exc
                logger.warning(
                    f"[AutoSwitch FAILOVER] Candidate '{candidate.name}' failed with {exc} "
                    f"(chunks emitted: {chunks_emitted})."
                )
                candidate.mark_failure(exc)
                self._parent.record_turn_failure(candidate, exc)

                # If tokens were already pushed to audio synthesis, retrying from scratch
                # would result in garbled audio overlap, so re-raise.
                if chunks_emitted > 0:
                    raise

                # Clean failover: 0 tokens were emitted, so next candidate can answer transparently!
                continue

            finally:
                if child_stream is not None:
                    try:
                        await child_stream.aclose()
                    except Exception:
                        pass

        if last_error:
            logger.error(f"[AutoSwitch CRITICAL] All candidate LLMs exhausted. Last error: {last_error}")
            raise last_error

        raise RuntimeError("No available LLM providers configured in ResilientAutoSwitchLLM pool.")


class ResilientAutoSwitchLLM(llm.LLM):
    """
    Production Auto-Switching LLM wrapper compliant with LiveKit Agent framework.
    Manages a pool of primary and fallback models across Google Gemini and Groq.
    """

    def __init__(self, candidates: List[LLMCandidate]):
        super().__init__()
        self.candidates = candidates
        self.total_failovers = 0
        self.last_failover_event: Optional[Dict[str, Any]] = None

    @property
    def model(self) -> str:
        for c in self.candidates:
            if c.is_available:
                return c.model
        return self.candidates[0].model if self.candidates else "unknown"

    @property
    def provider(self) -> str:
        for c in self.candidates:
            if c.is_available:
                return c.provider
        return self.candidates[0].provider if self.candidates else "unknown"

    def get_ordered_candidates(self) -> List[LLMCandidate]:
        """Returns available candidates sorted by priority."""
        return sorted(self.candidates, key=lambda c: (not c.is_available, c.priority))

    def record_turn_success(self, candidate: LLMCandidate):
        pass

    def record_turn_failure(self, failed_candidate: LLMCandidate, exc: Exception):
        self.total_failovers += 1
        self.last_failover_event = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "failed_candidate": failed_candidate.name,
            "error": str(exc)[:200],
        }

    def get_status_report(self) -> Dict[str, Any]:
        """Telemetry snapshot for dashboard."""
        return {
            "active_model": self.model,
            "active_provider": self.provider,
            "total_failovers": self.total_failovers,
            "last_failover_event": self.last_failover_event,
            "candidates": [c.to_dict() for c in self.candidates],
        }

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: Optional[List[Any]] = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls: Any = NOT_GIVEN,
        tool_choice: Any = NOT_GIVEN,
        extra_kwargs: Any = NOT_GIVEN,
        **kwargs: Any,
    ) -> ResilientLLMStream:
        return ResilientLLMStream(
            self,
            chat_ctx=chat_ctx,
            tools=tools,
            conn_options=conn_options,
            tool_choice=tool_choice,
            extra_kwargs=extra_kwargs,
            parallel_tool_calls=parallel_tool_calls,
            **kwargs,
        )


def build_resilient_llm() -> ResilientAutoSwitchLLM:
    """
    Factory function to construct the resilient LLM pool from environment variables.
    Collects all available Google API keys and Groq credentials.
    """
    candidates: List[LLMCandidate] = []

    # 1. Collect Google API Keys
    google_keys: List[str] = []
    
    # Primary key
    if Config.GOOGLE_API_KEY:
        google_keys.append(Config.GOOGLE_API_KEY)

    # Additional individual keys (GOOGLE_API_KEY_2, etc.)
    for var in ["GOOGLE_API_KEY_2", "GOOGLE_API_KEY_3", "GOOGLE_API_KEY_4"]:
        key_val = os.getenv(var)
        if key_val and key_val not in google_keys:
            google_keys.append(key_val)

    # Comma-separated list if provided (GOOGLE_API_KEYS=key1,key2,key3)
    keys_csv = os.getenv("GOOGLE_API_KEYS", "")
    if keys_csv:
        for k in keys_csv.split(","):
            k = k.strip()
            if k and k not in google_keys:
                google_keys.append(k)

    # Build Google Gemini candidates
    # Priority order:
    # Key 1 -> gemini-3.5-flash-lite
    # Key 1 -> gemini-3.5-flash
    # Key 1 -> gemini-flash-lite-latest
    # (Repeated for secondary keys if available)
    for idx, key in enumerate(google_keys):
        masked_key = f"...{key[-6:]}" if len(key) > 6 else "key"
        key_label = f"Key-{idx+1}"

        # 1. Primary fast model
        candidates.append(
            LLMCandidate(
                name=f"Google Gemini 3.5 Flash-Lite ({key_label})",
                provider="google",
                model="gemini-3.5-flash-lite",
                client=google.LLM(model="gemini-3.5-flash-lite", api_key=key),
                api_key_masked=masked_key,
                priority=1 + (idx * 3),
            )
        )

        # 2. Gemini 3.8 Flash
        candidates.append(
            LLMCandidate(
                name=f"Google Gemini 3.8 Flash ({key_label})",
                provider="google",
                model="gemini-3.8-flash",
                client=google.LLM(model="gemini-3.8-flash", api_key=key),
                api_key_masked=masked_key,
                priority=2 + (idx * 3),
            )
        )

    # 2. Add Cross-Provider Groq Pool (Multiple keys for continuous conversation)
    groq_keys: List[str] = []
    groq_vars = [k for k in os.environ.keys() if k.startswith("GROQ_API_KEY")]

    def _groq_key_sort(name: str) -> int:
        parts = name.split("_")
        if len(parts) >= 4 and parts[3].isdigit():
            return int(parts[3])
        return 1

    groq_vars.sort(key=_groq_key_sort)

    for var in groq_vars:
        val = os.getenv(var)
        if val and val.strip() and val.strip() not in groq_keys:
            groq_keys.append(val.strip())

    for idx, groq_key in enumerate(groq_keys):
        masked_groq = f"...{groq_key[-6:]}" if len(groq_key) > 6 else f"groq-{idx+1}"
        candidates.append(
            LLMCandidate(
                name=f"Groq Cloud gpt-oss-20b (Key-{idx+1})",
                provider="groq",
                model="openai/gpt-oss-20b",
                client=lk_openai.LLM(
                    model="openai/gpt-oss-20b",
                    base_url="https://api.groq.com/openai/v1",
                    api_key=groq_key,
                ),
                api_key_masked=masked_groq,
                priority=10 + idx,
            )
        )

    logger.info(f"Initialized ResilientAutoSwitchLLM with {len(candidates)} candidate configurations.")
    return ResilientAutoSwitchLLM(candidates)


# Shared global instance for telemetry / dashboard access
resilient_engine = build_resilient_llm()
