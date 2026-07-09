"""Pré-filtro barato (opcional) — heurísticas antes de gastar o LLM.

Ideia emprestada do cascade de projetos de triagem: evita chamar o modelo
para casos óbvios/baratos. Conservador de propósito — na dúvida, deixa passar
para o LLM decidir. NUNCA decide exclusão aqui (isso exige o modelo + sinais).
"""
from __future__ import annotations

from dataclasses import dataclass

from .gmail_client import EmailMsg


@dataclass
class Prefiltragem:
    pular_llm: bool          # se True, usa 'categoria' direto sem chamar o modelo
    categoria: str | None = None
    confianca: float = 0.0
    motivo: str = ""


def aplicar(email: EmailMsg) -> Prefiltragem:
    """Retorna Prefiltragem. Por padrão, não pula (deixa o LLM classificar).

    Mantido minimalista de propósito: heurísticas frágeis geram erro silencioso.
    Só marca casos muito seguros. Expandir com evidência dos logs (Fase 5).
    """
    # Placeholder conservador: hoje sempre delega ao LLM.
    # (Gancho pronto para, no futuro, atalhar ex.: remetentes conhecidos.)
    return Prefiltragem(pular_llm=False)
