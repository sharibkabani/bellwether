"""Pluggable LLM client for the AI trending signal.

The signal needs one thing from a model: given a system + user prompt, return a
JSON string. That's it — so any chat model works, and Bellwether stays free to
run by defaulting to open-source models.

One ``requests``-based client speaks the OpenAI-compatible ``/chat/completions``
API, which the popular free options all expose:

  • **Ollama** — runs open models (Llama 3.1, Qwen2.5, Mistral…) locally for $0,
    no API key, fully private. Base URL ``http://localhost:11434/v1``.
  • **Groq** — free API tier serving open models very fast. Needs a free key.
  • **OpenRouter** — has free model variants. Needs a key.

Anthropic (Claude) remains available as an optional provider for anyone who
wants it, but it is no longer required — the ``anthropic`` package is optional.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod

import requests

# Sensible defaults per provider: (base_url, default_model, key_env_var).
# Groq's free tier serving gpt-oss-120b is the best free option for this task:
# a 120B-class reasoner, free with one key (no card), and our ~100 calls/day are
# far under the free daily cap. Ollama is the best no-key local fallback.
PROVIDER_DEFAULTS = {
    "groq": ("https://api.groq.com/openai/v1", "openai/gpt-oss-120b", "GROQ_API_KEY"),
    "ollama": ("http://localhost:11434/v1", "qwen3:8b", ""),
    "openrouter": ("https://openrouter.ai/api/v1", "openai/gpt-oss-120b:free", "OPENROUTER_API_KEY"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini", "OPENAI_API_KEY"),
    "anthropic": ("", "claude-opus-4-8", "ANTHROPIC_API_KEY"),
}


def extract_json(text: str) -> str:
    """Pull a JSON object out of a model response, tolerating markdown fences
    and surrounding prose (open models don't always honor strict JSON mode)."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    # If it's already valid JSON, keep it; otherwise grab the outermost object.
    try:
        json.loads(t)
        return t
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start != -1 and end > start:
            return t[start : end + 1]
        return t


class LLMClient(ABC):
    name: str = "llm"

    @abstractmethod
    def complete_json(self, system: str, user: str, schema: dict | None = None) -> str:
        """Return the model's response as a JSON string."""

    @abstractmethod
    def complete_text(self, system: str, user: str) -> str:
        """Return the model's response as free-form text (e.g. a blog post)."""


class OpenAICompatibleClient(LLMClient):
    """Works against any OpenAI-compatible chat endpoint (Ollama, Groq,
    OpenRouter, OpenAI, vLLM, LM Studio, …)."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        max_tokens: int = 2000,
        name: str = "openai-compatible",
        timeout: int = 120,
    ):
        self.name = name
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._model = model
        self._api_key = api_key
        self._max_tokens = max_tokens
        self._timeout = timeout

    def complete_json(self, system: str, user: str, schema: dict | None = None) -> str:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": self._max_tokens,
            "temperature": 0.2,
            "stream": False,
            # Widely supported JSON mode; servers that ignore it still get the
            # explicit "respond with JSON" instruction in the prompt.
            "response_format": {"type": "json_object"},
        }
        resp = requests.post(self._url, headers=headers, json=body, timeout=self._timeout)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return extract_json(content)

    def complete_text(self, system: str, user: str) -> str:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": self._max_tokens,
            "temperature": 0.6,  # a touch of voice for prose
            "stream": False,
        }
        resp = requests.post(self._url, headers=headers, json=body, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


class AnthropicClient(LLMClient):
    """Optional Claude provider via the anthropic SDK (uses strict JSON schema
    output when a schema is supplied)."""

    def __init__(self, model: str, api_key: str, max_tokens: int = 2000):
        self.name = "anthropic"
        self._model = model
        self._max_tokens = max_tokens
        import anthropic  # raises ImportError if not installed → handled by factory

        self._client = anthropic.Anthropic(api_key=api_key)

    def complete_json(self, system: str, user: str, schema: dict | None = None) -> str:
        kwargs: dict = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if schema is not None:
            kwargs["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
        response = self._client.messages.create(**kwargs)
        for block in response.content:
            if block.type == "text" and block.text.strip():
                return extract_json(block.text)
        raise RuntimeError("no text block in response")

    def complete_text(self, system: str, user: str) -> str:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        for block in response.content:
            if block.type == "text" and block.text.strip():
                return block.text
        raise RuntimeError("no text block in response")


def build_client(
    provider: str,
    model: str = "",
    base_url: str = "",
    api_key: str = "",
    max_tokens: int = 2000,
) -> LLMClient | None:
    """Construct an LLM client for ``provider``, or None if it can't be built.

    Provider defaults fill in the base URL and model when not specified. The
    caller resolves the API key from the environment (Ollama needs none).
    """
    provider = provider.lower()
    default_base, default_model, _ = PROVIDER_DEFAULTS.get(provider, ("", "", ""))
    base_url = base_url or default_base
    model = model or default_model

    if provider == "anthropic":
        if not api_key:
            return None
        try:
            return AnthropicClient(model=model, api_key=api_key, max_tokens=max_tokens)
        except ImportError:
            return None  # anthropic package not installed; fall back to momentum

    if not base_url:
        return None
    # Groq/OpenRouter/OpenAI require a key; Ollama and local servers don't.
    if provider in ("groq", "openrouter", "openai") and not api_key:
        return None
    return OpenAICompatibleClient(
        base_url=base_url, model=model, api_key=api_key, max_tokens=max_tokens, name=provider
    )
