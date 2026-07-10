"""Cliente Gmail — escopo gmail.modify apenas (label + archive + trash).

Versão da integração HA: o `service` (googleapiclient) é SEMPRE injetado —
quem constrói é o motor, com o access token gerenciado pelo Home Assistant
(OAuth2Session renova sozinho). Não há token.json nem login próprio aqui.

Nunca faz delete permanente: exclusão é sempre users.messages.trash.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field


@dataclass
class EmailMsg:
    """Visão enxuta de uma mensagem, pronta para o classificador."""
    id: str
    thread_id: str
    remetente: str = ""
    assunto: str = ""
    destinatario: str = ""
    data: str = ""
    tem_list_unsubscribe: bool = False   # sinal determinístico p/ exclusão
    corpo: str = ""                       # texto (não confiável — entrada do LLM)
    label_ids: list[str] = field(default_factory=list)


class GmailClient:
    def __init__(self, service):
        self.service = service
        self._labels_cache: dict[str, str] | None = None  # nome -> id

    # --------------------------------------------------------------- perfil
    def get_profile(self) -> dict:
        """messagesTotal + historyId atual (usado no bootstrap e diagnóstico)."""
        return self.service.users().getProfile(userId="me").execute()

    # --------------------------------------------------------------- labels
    def _carregar_labels(self) -> dict[str, str]:
        if self._labels_cache is None:
            resp = self.service.users().labels().list(userId="me").execute()
            self._labels_cache = {l["name"]: l["id"] for l in resp.get("labels", [])}
        return self._labels_cache

    def garantir_label(self, nome: str) -> str:
        """Retorna o id da label, criando-a se não existir (aninhamento por '/')."""
        cache = self._carregar_labels()
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
        Levanta HistoryExpirada (404) quando o cursor é antigo demais — o
        chamador cai no fallback messages.list (ver motor).
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
                raise HistoryExpirada() from e
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

    def contar_mensagens_thread(self, thread_id: str) -> int:
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
        corpo = GmailClient._extrair_corpo(payload)
        return EmailMsg(
            id=raw["id"],
            thread_id=raw.get("threadId", ""),
            remetente=headers.get("from", ""),
            assunto=headers.get("subject", ""),
            destinatario=headers.get("to", ""),
            data=headers.get("date", ""),
            tem_list_unsubscribe="list-unsubscribe" in headers,
            corpo=corpo,
            label_ids=raw.get("labelIds", []),
        )

    @staticmethod
    def _extrair_corpo(payload: dict, limite: int = 4000) -> str:
        """Extrai texto de text/plain (preferido) ou text/html, recursivo em multipart."""
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
                texto = GmailClient._extrair_corpo(p, limite)
                if texto:
                    return texto
        if mime == "text/html" and body.get("data"):
            return decode(body["data"])[:limite]
        return ""

    # -------------------------------------------------------- modificações
    def modificar(
        self, msg_id: str, add: list[str] | None = None, remove: list[str] | None = None
    ) -> None:
        self.service.users().messages().modify(
            userId="me",
            id=msg_id,
            body={"addLabelIds": add or [], "removeLabelIds": remove or []},
        ).execute()

    def trash(self, msg_id: str) -> None:
        """Manda para a Lixeira (recuperável ~30 dias). NUNCA delete permanente."""
        self.service.users().messages().trash(userId="me", id=msg_id).execute()


class HistoryExpirada(Exception):
    """startHistoryId antigo demais (Gmail retornou 404). Chamador usa fallback."""
