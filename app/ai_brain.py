"""
app/ai_brain.py

AI model abstraction layer.

Production hardening applied:
  - Singleton client instances (one per provider) — avoids creating a new
    HTTP connection on every call, which added measurable latency per clip.
  - Retry with exponential back-off — handles transient rate-limit (429)
    and network hiccups without crashing the pipeline.
  - Graceful error on missing API key — raises ValueError with actionable
    message instead of bare KeyError.
  - complete_json retries on malformed JSON — models occasionally wrap
    JSON in markdown; the cleaner + retry loop handles this.

Supported providers
───────────────────
  claude   — Anthropic Claude API (best quality, paid)
             Requires: ANTHROPIC_API_KEY
             Model:    CLAUDE_MODEL (default: claude-haiku-4-5-20251001)

  groq     — Groq cloud inference (free tier, very fast)
             Requires: GROQ_API_KEY
             Model:    GROQ_MODEL (default: llama-3.3-70b-versatile)

  ollama   — Local Ollama server (free, runs on your machine)
             Base URL: OLLAMA_BASE_URL (default: http://localhost:11434)
             Model:    OLLAMA_MODEL (default: llama3.2)
"""
import json
import logging
import os
import time
from functools import lru_cache
from typing import Union

logger = logging.getLogger(__name__)

# ── Retry config ───────────────────────────────────────────────────────────────
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 2.0   # seconds; doubles each attempt


# ── Helpers ────────────────────────────────────────────────────────────────────

def _clean_json(raw: str) -> str:
    """Strip markdown code fences that some models wrap JSON in."""
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    return raw.strip()


def _get_provider() -> str:
    return os.getenv("AI_PROVIDER", "claude").lower().strip()


def _retry(fn, label: str):
    """
    Call fn() up to _MAX_RETRIES times with exponential back-off.
    Re-raises the last exception if all attempts fail.
    """
    last_exc = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES:
                delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    "[AI Brain] %s attempt %d/%d failed (%s). Retrying in %.1fs...",
                    label, attempt, _MAX_RETRIES, exc, delay,
                )
                time.sleep(delay)
            else:
                logger.error("[AI Brain] %s failed after %d attempts.", label, _MAX_RETRIES)
    raise last_exc


# ── Backend: Claude ────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _get_anthropic_client():
    """Singleton Anthropic client — created once, reused for all calls."""
    import anthropic
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key or key.startswith("your_"):
        raise ValueError(
            "ANTHROPIC_API_KEY is not set. "
            "Add it to your .env file or set AI_PROVIDER=groq to use the free tier."
        )
    return anthropic.Anthropic(api_key=key)


def _complete_claude(prompt: str, max_tokens: int) -> str:
    model = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
    client = _get_anthropic_client()

    def _call():
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text

    return _retry(_call, f"Claude ({model})")


# ── Backend: Groq ──────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _get_groq_client():
    """Singleton Groq client — created once, reused for all calls."""
    from groq import Groq
    key = os.getenv("GROQ_API_KEY", "")
    if not key or key.startswith("your_"):
        raise ValueError(
            "GROQ_API_KEY is not set. "
            "Get a free key at https://console.groq.com and add it to .env."
        )
    return Groq(api_key=key)


def _complete_groq(prompt: str, max_tokens: int) -> str:
    model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    client = _get_groq_client()

    def _call():
        response = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
        )
        return response.choices[0].message.content

    return _retry(_call, f"Groq ({model})")


# ── Backend: Ollama ────────────────────────────────────────────────────────────

def _complete_ollama(prompt: str, max_tokens: int) -> str:
    import httpx
    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    model    = os.getenv("OLLAMA_MODEL", "llama3.2")

    def _call():
        response = httpx.post(
            f"{base_url}/api/generate",
            json={
                "model":   model,
                "prompt":  prompt,
                "stream":  False,
                "options": {"num_predict": max_tokens},
            },
            timeout=120,
        )
        response.raise_for_status()
        return response.json()["response"]

    return _retry(_call, f"Ollama ({model})")


# ── Public API ─────────────────────────────────────────────────────────────────

def complete(prompt: str, max_tokens: int = 2000) -> str:
    """
    Send a prompt to the configured AI provider and return the response text.
    This is the only function the rest of the app should call.
    """
    provider = _get_provider()
    logger.debug("[AI Brain] Provider: %s | max_tokens: %d", provider, max_tokens)

    if provider == "claude":
        return _complete_claude(prompt, max_tokens)
    if provider == "groq":
        return _complete_groq(prompt, max_tokens)
    if provider == "ollama":
        return _complete_ollama(prompt, max_tokens)

    raise ValueError(
        f"Unknown AI_PROVIDER: '{provider}'. Valid options: claude, groq, ollama"
    )


def complete_json(prompt: str, max_tokens: int = 2000) -> Union[dict, list]:
    """
    Like complete(), but parses and returns the response as JSON.
    Retries up to 2 times if the model returns malformed JSON.
    Raises json.JSONDecodeError only if all retries are exhausted.
    """
    last_exc = None
    for attempt in range(1, 3):
        raw = complete(prompt, max_tokens)
        cleaned = _clean_json(raw)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            last_exc = exc
            logger.warning(
                "[AI Brain] JSON parse failed (attempt %d/2). Raw: %.200s...",
                attempt, raw,
            )
    raise last_exc


def get_provider_info() -> dict:
    """Return current provider info for the dashboard and setup check."""
    provider = _get_provider()
    info: dict = {"provider": provider}

    if provider == "claude":
        key = os.getenv("ANTHROPIC_API_KEY", "")
        info["model"] = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
        info["configured"] = bool(key and not key.startswith("your_"))

    elif provider == "groq":
        key = os.getenv("GROQ_API_KEY", "")
        info["model"] = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
        info["configured"] = bool(key and not key.startswith("your_"))
        info["free"] = True

    elif provider == "ollama":
        info["model"] = os.getenv("OLLAMA_MODEL", "llama3.2")
        info["base_url"] = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        info["configured"] = True
        info["free"] = True

    return info