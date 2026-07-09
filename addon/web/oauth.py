"""OAuth em dois passos para o wizard (sem terminal, sem túnel SSH).

O app do Google é do tipo *Desktop*, cujo único redirect permitido é o loopback
(`http://localhost:PORT`). Através do ingress do HA, o navegador do usuário não
está no host do add-on, então nada escuta esse loopback — a página de retorno
"falha" (erro de conexão). Mas a URL de retorno carrega o `code` na barra de
endereço; o usuário copia essa URL e cola no wizard, que troca o code pelo token.

É o fluxo mais simples que funciona com o escopo sensível `gmail.modify`
(o device flow do Google — código curto no celular — NÃO permite escopos Gmail).

Dois passos:
  1. `gerar_url(credentials_path)` → (url de consentimento, state)
  2. `concluir(credentials_path, state, resposta_url, token_path)` → grava token.json
"""
from __future__ import annotations

import os

# Loopback é http (não https); para o oauthlib aceitar o retorno local.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
# Google pode devolver os escopos em ordem/forma diferente; não tratar como erro.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

from src.gmail_client import SCOPES  # mesma lista do núcleo (gmail.modify)


def redirect_uri() -> str:
    """Loopback fixo. A porta é irrelevante (nada escuta), mas precisa ser a
    mesma entre gerar a URL e trocar o code."""
    porta = os.environ.get("OAUTH_PORT", "8765")
    return f"http://localhost:{porta}"


def _flow(credentials_path: str, state: str | None = None):
    from google_auth_oauthlib.flow import Flow
    return Flow.from_client_secrets_file(
        credentials_path, scopes=SCOPES,
        redirect_uri=redirect_uri(), state=state)


def gerar_url(credentials_path: str) -> tuple[str, str]:
    """Passo 1: URL de consentimento do Google. `access_type=offline` +
    `prompt=consent` garantem o refresh_token (login que não expira)."""
    if not os.path.exists(credentials_path):
        raise FileNotFoundError(credentials_path)
    flow = _flow(credentials_path)
    url, state = flow.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="false")
    return url, state


def concluir(credentials_path: str, state: str, resposta_url: str,
             token_path: str) -> None:
    """Passo 2: troca o code (extraído da URL colada) pelo token e o grava."""
    resposta_url = (resposta_url or "").strip()
    if "code=" not in resposta_url:
        raise ValueError(
            "A URL colada não contém o código de autorização (code=...). "
            "Copie a URL INTEIRA da barra de endereço depois de aprovar.")
    flow = _flow(credentials_path, state=state)
    flow.fetch_token(authorization_response=resposta_url)
    creds = flow.credentials
    if not creds.refresh_token:
        raise ValueError(
            "O Google não devolveu um refresh_token. Refaça o login "
            "(revogue o acesso antigo em myaccount.google.com se necessário).")
    os.makedirs(os.path.dirname(token_path), exist_ok=True)
    with open(token_path, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
