"""Cheap optional pre-filter — heuristics before spending LLM calls.

Cascade idea borrowed from triage projects: avoids calling the model for
obvious/cheap cases. Deliberately conservative — when in doubt, let the LLM
decide. NEVER decides trashing here (that requires the model + signals).
"""
from __future__ import annotations

from dataclasses import dataclass

from .gmail_client import EmailMsg


@dataclass
class Prefilter:
    skip_llm: bool          # if True, use 'category' directly without calling the model
    category: str | None = None
    confidence: float = 0.0
    reason: str = ""


def apply(email: EmailMsg) -> Prefilter:
    """Returns Prefilter. By default it does not skip (LLM classifies).

    Kept minimal on purpose: fragile heuristics produce silent errors.
    Only flag very safe cases. Expand with evidence from the logs.
    """
    # Conservative placeholder: currently always delegates to the LLM.
    # (Hook ready to short-circuit e.g. known senders in the future.)
    return Prefilter(skip_llm=False)
