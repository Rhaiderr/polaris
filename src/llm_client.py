"""Cliente LLM — qualquer endpoint OpenAI-compatible (/v1/chat/completions).

Genérico por design: funciona com LM Studio, Ollama, llama.cpp, vLLM,
OpenRouter, OpenAI, etc. O código NUNCA menciona um provedor específico —
tudo vem de LLM_BASE_URL / LLM_MODEL / LLM_API_KEY no ambiente.

Sem tool-calling (o contrato é JSON no text — ver classificador), porque
tool-calling é justamente o que falha em vários modelos locais.
"""
from __future__ import annotations

import os

import requests


class LLMUnavailable(Exception):
    """O endpoint não respondeu. O orquestrador trata como 'pular execução'."""


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
            raise ValueError("LLM_BASE_URL e LLM_MODEL são obrigatórios (ver .env).")

    def available(self) -> bool:
        """Ping barato: lista modelos. False se o endpoint não está de pé."""
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
        """Uma rodada system+user. Retorna o text da response (bruto).

        Levanta LLMUnavailable em erro de conexão/timeout — o orquestrador
        decide pular a execução (o incremental recupera na próxima rodada).
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
