"""Cheap optional pre-filter — heuristics before spending LLM calls.

Cascade idea borrowed from triage projects: avoids calling the model for
obvious/cheap cases. Deliberately conservative — when in doubt, let the LLM
decide. NEVER decides trashing here (that requires the model + signals).
"""
from __future__ import annotations

from dataclasses import dataclass

from .gmail_client import EmailMsg


@dataclass
class Prefiltragem:
    pular_llm: bool          # if True, use 'categoria' directly without calling the model
    categoria: str | None = None
    confianca: float = 0.0
    motivo: str = ""


def aplicar(email: EmailMsg) -> Prefiltragem:
    """Returns Prefiltragem. By default it does not skip (LLM classifies).

    Kept minimal on purpose: fragile heuristics produce silent errors.
    Only flag very safe cases. Expand with evidence from the logs.
    """
    # Conservative placeholder: currently always delegates to the LLM.
    # (Hook ready to short-circuit e.g. known senders in the future.)
    return Prefiltragem(pular_llm=False)
