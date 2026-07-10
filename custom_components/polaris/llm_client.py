"""Cliente LLM — qualquer endpoint OpenAI-compatible (/v1/chat/completions).

Genérico por design: funciona com LM Studio, Ollama, llama.cpp, vLLM,
OpenRouter, OpenAI, etc. Na integração HA, base_url/model/api_key vêm das
OPÇÕES da config entry (não de env vars).

Sem tool-calling (o contrato é JSON no texto — ver classificador), porque
tool-calling é justamente o que falha em vários modelos locais.

Chamadas são bloqueantes (requests) — o runtime roda tudo em executor.
"""
from __future__ import annotations

import requests


class LLMIndisponivel(Exception):
    """O endpoint não respondeu. O runtime trata como 'pular execução'."""


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
            raise ValueError("Endpoint e modelo do LLM são obrigatórios "
                             "(configure nas opções da integração).")

    def disponivel(self) -> bool:
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
        """Uma rodada system+user. Retorna o texto da resposta (bruto).

        Levanta LLMIndisponivel em erro de conexão/timeout — o runtime
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
            raise LLMIndisponivel(str(e)) from e
        if r.status_code != 200:
            raise LLMIndisponivel(f"HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        return data["choices"][0]["message"]["content"]
