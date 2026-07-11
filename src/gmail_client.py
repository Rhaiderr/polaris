"""Cliente Gmail — escopo gmail.modify apenas (label + archive + trash).

Responsável por toda conversa com a Gmail API:
- OAuth (carrega credentials/token; 1º login gera token.json fora do container);
- sincronização incremental robusta (History API) com bootstrap via getProfile,
  fallback automático quando o historyId expira (404) e historyTypes=messageAdded
  (anti-retroalimentação: as próprias ações do Polaris não são reprocessadas);
- sincronização completa (messages.list paginado);
- aplicação de labels em lote (batchModify), arquivamento e trash;
- extração do conteúdo relevante de cada mensagem (headers + corpo).

Nunca faz delete permanente: exclusão é sempre users.messages.trash.
"""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field

# NB: as bibliotecas do Google são importadas de forma LAZY (dentro dos métodos
# que as usam). Assim, importar este módulo só para EmailMsg/HistoryExpired
# (ex.: no sanity check offline tests/dry_run.py) não exige o pacote instalado.

# Escopo mínimo: ler + modify (label/archive/trash). NÃO inclui delete nem send.
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config")
# credentials.json é COMPARTILHADO entre contas: um app OAuth Desktop autoriza
# quantas contas Google você quiser. O token.json é por-conta (config/<conta>/).
CREDENTIALS_PATH = os.path.join(CONFIG_DIR, "credentials.json")


@dataclass
class EmailMsg:
    """Lean view of a message, ready for the classifier."""
    id: str
    thread_id: str
    sender: str = ""
    subject: str = ""
    recipient: str = ""
    date: str = ""
    has_list_unsubscribe: bool = False   # deterministic signal for trashing
    body: str = ""                        # text (untrusted — LLM input)
    label_ids: list[str] = field(default_factory=list)


class GmailClient:
    def __init__(self, token_path: str, credentials_path: str = CREDENTIALS_PATH,
                 service=None):
        self.token_path = token_path
        self.credentials_path = credentials_path
        self.service = service or self._build_service()
        self._labels_cache: dict[str, str] | None = None  # nome -> id

    # ----------------------------------------------------------------- auth
    def _load_creds(self):
        from google.oauth2.credentials import Credentials
        if os.path.exists(self.token_path):
            return Credentials.from_authorized_user_file(self.token_path, SCOPES)
        return None

    @classmethod
    def authenticate_interactive(cls, token_path: str,
                              credentials_path: str = CREDENTIALS_PATH) -> None:
        """1º login OAuth de uma conta (roda FORA do container, 1 vez).

        Grava o token em `token_path` (config/<conta>/token.json). Requer o
        credentials.json COMPARTILHADO (ver docs/gerar-credenciais-gmail.md).
        Sobe um servidor local numa porta FIXA (env OAUTH_PORT, default 8765) e
        NÃO tenta abrir navegador — imprime a URL de consentimento. Assim funciona
        em máquina headless / via SSH: basta encaminhar a porta
        (ssh -L PORT:localhost:PORT ...) e abrir a URL no navegador da sua máquina.
        """
        from google_auth_oauthlib.flow import InstalledAppFlow
        if not os.path.exists(credentials_path):
            raise FileNotFoundError(
                f"Falta {credentials_path}. Siga docs/gerar-credenciais-gmail.md."
            )
        os.makedirs(os.path.dirname(token_path), exist_ok=True)
        port = int(os.environ.get("OAUTH_PORT", "8765"))
        flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
        creds = flow.run_local_server(
            port=port,
            open_browser=False,
            authorization_prompt_message=(
                "Abra esta URL no navegador da sua máquina "
                f"(com túnel SSH: ssh -L {port}:localhost:{port} SEU_HOST):\n{{url}}"
            ),
        )
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    def _build_service(self):
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        creds = self._load_creds()
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                with open(self.token_path, "w") as f:
                    f.write(creds.to_json())
            else:
                raise RuntimeError(
                    "Sem token OAuth válido. Rode o 1º login: "
                    "python -m src.orquestrador --conta <nome> --login  (fora do container)."
                )
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    # --------------------------------------------------------------- perfil
    def get_profile(self) -> dict:
        """messagesTotal + historyId atual (usado no bootstrap e diagnóstico)."""
        return self.service.users().getProfile(userId="me").execute()

    # --------------------------------------------------------------- labels
    def _load_labels(self) -> dict[str, str]:
        if self._labels_cache is None:
            resp = self.service.users().labels().list(userId="me").execute()
            self._labels_cache = {l["name"]: l["id"] for l in resp.get("labels", [])}
        return self._labels_cache

    def ensure_label(self, nome: str) -> str:
        """Retorna o id da label, criando-a se não existir (aninhamento por '/')."""
        cache = self._load_labels()
        if nome in cache:
            return cache[nome]
        criada = (
            self.service.users()
            .labels()
            .create(
                userId="me",
                body={
                    "name": nome,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
            .execute()
        )
        cache[nome] = criada["id"]
        return criada["id"]

    # ---------------------------------------------------- sincronização
    def history_added(self, start_history_id: str) -> tuple[list[dict], str | None]:
        """IDs de mensagens ADICIONADAS desde start_history_id.

        Retorna (lista de {'id','threadId'}, novo_history_id).
        Levanta HistoryExpired (404) quando o cursor é antigo demais — o
        chamador cai no fallback messages.list (ver orquestrador).
        historyTypes=messageAdded evita reprocessar as ações do próprio Polaris.
        """
        from googleapiclient.errors import HttpError
        msgs: dict[str, dict] = {}
        novo_hid: str | None = None
        page_token = None
        try:
            while True:
                resp = (
                    self.service.users()
                    .history()
                    .list(
                        userId="me",
                        startHistoryId=start_history_id,
                        historyTypes=["messageAdded"],
                        pageToken=page_token,
                    )
                    .execute()
                )
                novo_hid = resp.get("historyId", novo_hid)
                for h in resp.get("history", []):
                    for ma in h.get("messagesAdded", []):
                        m = ma["message"]
                        # ignora rascunhos/enviados e a própria Lixeira
                        labels = m.get("labelIds", [])
                        if "DRAFT" in labels or "SENT" in labels or "TRASH" in labels:
                            continue
                        msgs[m["id"]] = {"id": m["id"], "threadId": m["threadId"]}
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
        except HttpError as e:
            if e.resp.status == 404:
                raise HistoryExpired() from e
            raise
        return list(msgs.values()), novo_hid

    def messages_list(self, query: str, max_results: int | None = None) -> list[dict]:
        """messages.list paginado. Retorna [{'id','threadId'}]. query no estilo Gmail."""
        out: list[dict] = []
        page_token = None
        while True:
            resp = (
                self.service.users()
                .messages()
                .list(userId="me", q=query, pageToken=page_token, maxResults=500)
                .execute()
            )
            out.extend(resp.get("messages", []) or [])
            if max_results and len(out) >= max_results:
                return out[:max_results]
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return out

    # ----------------------------------------------------- leitura de msg
    def get_meta(self, msg_id: str) -> dict:
        """Só remetente/assunto (format=metadata) — barato para varreduras
        em massa (ex.: sugestão de categorias), sem baixar o corpo."""
        raw = (
            self.service.users()
            .messages()
            .get(userId="me", id=msg_id, format="metadata",
                 metadataHeaders=["From", "Subject"])
            .execute()
        )
        headers = {h["name"].lower(): h["value"]
                   for h in raw.get("payload", {}).get("headers", [])}
        return {"id": msg_id,
                "remetente": headers.get("from", ""),
                "assunto": headers.get("subject", "")}

    def get_email(self, msg_id: str) -> EmailMsg:
        raw = (
            self.service.users()
            .messages()
            .get(userId="me", id=msg_id, format="full")
            .execute()
        )
        return self._parse_email(raw)

    def count_thread_messages(self, thread_id: str) -> int:
        """Nº de mensagens na thread (regra: arquivar/trash só em thread única)."""
        t = (
            self.service.users()
            .threads()
            .get(userId="me", id=thread_id, format="minimal")
            .execute()
        )
        return len(t.get("messages", []))

    @staticmethod
    def _parse_email(raw: dict) -> EmailMsg:
        payload = raw.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        body = GmailClient._extract_body(payload)
        return EmailMsg(
            id=raw["id"],
            thread_id=raw.get("threadId", ""),
            sender=headers.get("from", ""),
            subject=headers.get("subject", ""),
            recipient=headers.get("to", ""),
            date=headers.get("date", ""),
            has_list_unsubscribe="list-unsubscribe" in headers,
            body=body,
            label_ids=raw.get("labelIds", []),
        )

    @staticmethod
    def _extract_body(payload: dict, limite: int = 4000) -> str:
        """Extrai text de text/plain (preferido) ou text/html, recursivo em multipart."""
        def decode(data: str) -> str:
            return base64.urlsafe_b64decode(data.encode()).decode("utf-8", "replace")

        mime = payload.get("mimeType", "")
        body = payload.get("body", {})
        if mime == "text/plain" and body.get("data"):
            return decode(body["data"])[:limite]
        if mime.startswith("multipart/"):
            # tenta plain antes de html
            partes = payload.get("parts", [])
            for alvo in ("text/plain", "text/html"):
                for p in partes:
                    if p.get("mimeType") == alvo and p.get("body", {}).get("data"):
                        return decode(p["body"]["data"])[:limite]
            # multipart aninhado
            for p in partes:
                text = GmailClient._extract_body(p, limite)
                if text:
                    return text
        if mime == "text/html" and body.get("data"):
            return decode(body["data"])[:limite]
        return ""

    # -------------------------------------------------------- modificações
    def modify(
        self, msg_id: str, add: list[str] | None = None, remove: list[str] | None = None
    ) -> None:
        self.service.users().messages().modify(
            userId="me",
            id=msg_id,
            body={"addLabelIds": add or [], "removeLabelIds": remove or []},
        ).execute()

    def batch_modify(
        self, ids: list[str], add: list[str] | None = None, remove: list[str] | None = None
    ) -> None:
        """Aplica as MESMAS labels a vários ids de uma vez (economiza chamadas)."""
        if not ids:
            return
        for i in range(0, len(ids), 1000):  # limite da API: 1000 ids por chamada
            self.service.users().messages().batchModify(
                userId="me",
                body={
                    "ids": ids[i : i + 1000],
                    "addLabelIds": add or [],
                    "removeLabelIds": remove or [],
                },
            ).execute()

    def trash(self, msg_id: str) -> None:
        """Manda para a Lixeira (recuperável ~30 dias). NUNCA delete permanente."""
        self.service.users().messages().trash(userId="me", id=msg_id).execute()


class HistoryExpired(Exception):
    """startHistoryId antigo demais (Gmail retornou 404). Chamador usa fallback."""
