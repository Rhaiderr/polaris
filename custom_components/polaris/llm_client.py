"""LLM client — any OpenAI-compatible endpoint (/v1/chat/completions).

Generic by design: works with LM Studio, Ollama, llama.cpp, vLLM,
OpenRouter, OpenAI, etc. In the HA integration, base_url/model/api_key come
from the config entry OPTIONS (not env vars).

No tool-calling (the contract is JSON-in-text — see classificador), because
tool-calling is exactly what breaks on many local models.

Calls are blocking (requests) — the runtime dispatches through the executor.
"""
from __future__ import annotations

import requests


class LLMUnavailable(Exception):
    """The endpoint did not respond. The runtime treats it as 'skip the run'."""


class LLMClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        temperature: float = 0.0,
        max_tokens: int = 400,
        timeout: int = 120,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        if not self.base_url or not self.model:
            raise ValueError("The LLM endpoint and model are required "
                             "(set them in the integration options).")

    def available(self) -> bool:
        """Cheap ping: lists models. False when the endpoint is down."""
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

        Raises LLMUnavailable on connection errors/timeouts — the runtime
        skips the run (the incremental mode catches up next time).
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
