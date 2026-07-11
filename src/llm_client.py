"""LLM client — any OpenAI-compatible endpoint (/v1/chat/completions).

Generic by design: works with LM Studio, Ollama, llama.cpp, vLLM,
OpenRouter, OpenAI, etc. The code NEVER mentions a specific provider —
everything comes from LLM_BASE_URL / LLM_MODEL / LLM_API_KEY in the environment.

No tool-calling (the contract is JSON in the text — see classificador), because
tool-calling is precisely what fails on several local models.
"""
from __future__ import annotations

import os

import requests


class LLMUnavailable(Exception):
    """The endpoint did not respond. The orchestrator treats it as 'skip the run'."""


class LLMClient:
    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: int | None = None,
    ):
        self.base_url = (base_url or os.environ.get("LLM_BASE_URL", "")).rstrip("/")
        self.model = model or os.environ.get("LLM_MODEL", "")
        self.api_key = api_key if api_key is not None else os.environ.get("LLM_API_KEY", "")
        self.temperature = (
            temperature if temperature is not None
            else float(os.environ.get("LLM_TEMPERATURE", "0.0"))
        )
        self.max_tokens = max_tokens or int(os.environ.get("LLM_MAX_TOKENS", "400"))
        self.timeout = timeout or int(os.environ.get("LLM_TIMEOUT", "120"))
        if not self.base_url or not self.model:
            raise ValueError("LLM_BASE_URL and LLM_MODEL are required (see .env).")

    def available(self) -> bool:
        """Cheap ping: list models. False if the endpoint is down."""
        try:
            r = requests.get(f"{self.base_url}/models", headers=self._headers(), timeout=5)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def chat(self, system: str, user: str) -> str:
        """One system+user round. Returns the raw response text.

        Raises LLMUnavailable on connection/timeout error — the orchestrator
        decides to skip the run (the incremental one catches up next time).
        """
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        try:
            r = requests.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise LLMUnavailable(str(e)) from e
        if r.status_code != 200:
            raise LLMUnavailable(f"HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        return data["choices"][0]["message"]["content"]
