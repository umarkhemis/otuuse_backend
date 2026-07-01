"""
app/services/agent/llm_client.py
---------------------------------
Unified LLM client.
Primary: Groq (fast, free tier - ideal for development)
Fallback: Anthropic Claude (production-grade)

Switching between providers is a single env var change:
  LLM_PROVIDER=groq      -> uses Groq
  LLM_PROVIDER=anthropic -> uses Anthropic Claude

Both providers share the same interface so the rest of the
codebase never needs to know which one is active.
"""

from typing import Optional
import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Groq configuration
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_DEFAULT_MODEL = "llama-3.1-8b-instant"   # fast and capable for classification

# Anthropic configuration (for production switch)
ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
ANTHROPIC_DEFAULT_MODEL = "claude-haiku-4-5-20251001"


class LLMError(Exception):
    pass


class LLMClient:
    """
    Unified client for Groq and Anthropic.
    Always use the complete() method - it routes to the right provider.
    """

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict],
        max_tokens: int = 1000,
        temperature: float = 0.3,   # lower = more consistent/predictable responses
    ) -> str:
        """
        Send a completion request to the configured LLM provider.

        messages: list of {"role": "user"|"assistant", "content": "..."}
        Returns the assistant's reply as a plain string.
        """
        provider = getattr(settings, "LLM_PROVIDER", "groq")

        if provider == "groq":
            return await self._groq_complete(
                system_prompt=system_prompt,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        elif provider == "anthropic":
            return await self._anthropic_complete(
                system_prompt=system_prompt,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        else:
            raise LLMError(f"Unknown LLM provider: {provider}")

    # ── Groq ───────────────────────────────────────────────────────────────────

    async def _groq_complete(
        self,
        system_prompt: str,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
    ) -> str:
        """
        Groq uses the OpenAI-compatible chat completions API.
        System prompt is injected as the first message with role="system".
        """
        api_key = getattr(settings, "GROQ_API_KEY", "")
        if not api_key:
            raise LLMError("GROQ_API_KEY is not set in environment.")

        model = getattr(settings, "GROQ_MODEL", GROQ_DEFAULT_MODEL)

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                *messages,
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{GROQ_BASE_URL}/chat/completions",
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()

            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})

            logger.debug(
                "groq_completion",
                model=model,
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
            )

            return content.strip()

        except httpx.HTTPStatusError as e:
            logger.error("groq_http_error", status=e.response.status_code, body=e.response.text)
            raise LLMError(f"Groq API error {e.response.status_code}: {e.response.text}")
        except httpx.HTTPError as e:
            logger.error("groq_connection_error", error=str(e))
            raise LLMError(f"Cannot reach Groq API: {str(e)}")
        except (KeyError, IndexError) as e:
            logger.error("groq_parse_error", error=str(e))
            raise LLMError("Unexpected response format from Groq")

    # ── Anthropic ──────────────────────────────────────────────────────────────

    async def _anthropic_complete(
        self,
        system_prompt: str,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
    ) -> str:
        """
        Anthropic Messages API.
        System prompt is a top-level field, separate from messages.
        message roles: "user" | "assistant" (not "system")
        """
        api_key = settings.ANTHROPIC_API_KEY
        if not api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set in environment.")

        model = settings.LLM_MODEL

        # Anthropic requires messages to alternate user/assistant.
        # Filter out any "system" role messages from history.
        clean_messages = [
            m for m in messages
            if m.get("role") in ("user", "assistant")
        ]

        # Ensure the last message is from the user (Anthropic requirement)
        if not clean_messages or clean_messages[-1]["role"] != "user":
            clean_messages.append({"role": "user", "content": "Continue."})

        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system_prompt,
            "messages": clean_messages,
        }

        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{ANTHROPIC_BASE_URL}/messages",
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()

            content = data["content"][0]["text"]

            logger.debug(
                "anthropic_completion",
                model=model,
                input_tokens=data.get("usage", {}).get("input_tokens"),
                output_tokens=data.get("usage", {}).get("output_tokens"),
            )

            return content.strip()

        except httpx.HTTPStatusError as e:
            logger.error("anthropic_http_error", status=e.response.status_code, body=e.response.text)
            raise LLMError(f"Anthropic API error {e.response.status_code}: {e.response.text}")
        except httpx.HTTPError as e:
            logger.error("anthropic_connection_error", error=str(e))
            raise LLMError(f"Cannot reach Anthropic API: {str(e)}")
        except (KeyError, IndexError) as e:
            logger.error("anthropic_parse_error", error=str(e))
            raise LLMError("Unexpected response format from Anthropic")


# ── Singleton ──────────────────────────────────────────────────────────────────
llm_client = LLMClient()
