"""Gmail client — gmail.modify scope only (label + archive + trash).

HA integration flavor: talks to the Gmail REST API directly over HTTPS with
`requests`, using the access token managed by Home Assistant (OAuth2Session
refreshes it). No google-api-python-client / oauth2client dependency — those
drag in a heavy, fragile chain (oauth2client → pycryptodome → cffi) that
breaks on some HA runtimes. A single fresh token is injected per run.

Never deletes permanently: trashing is always users.messages.trash.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field

import requests

API = "https://gmail.googleapis.com/gmail/v1/users/me"


@dataclass
class EmailMsg:
    """Lean view of a message, ready for the classifier."""
    id: str
    thread_id: str
    remetente: str = ""
    assunto: str = ""
    destinatario: str = ""
    data: str = ""
    tem_list_unsubscribe: bool = False   # deterministic signal for trashing
    corpo: str = ""                       # text (untrusted — LLM input)
    label_ids: list[str] = field(default_factory=list)


class HistoryExpirada(Exception):
    """startHistoryId too old (Gmail returned 404). Caller uses the fallback."""


class GmailClient:
    def __init__(self, access_token: str, timeout: int = 30):
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {access_token}"
        self.timeout = timeout
        self._labels_cache: dict[str, str] | None = None  # name -> id

    # ------------------------------------------------------------- HTTP
    def _get(self, path: str, params: dict | None = None,
             allow_404: bool = False) -> dict:
        r = self.session.get(f"{API}{path}", params=params, timeout=self.timeout)
        if allow_404 and r.status_code == 404:
            raise HistoryExpirada()
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict | None = None) -> dict:
        r = self.session.post(f"{API}{path}", json=body or {}, timeout=self.timeout)
        r.raise_for_status()
        return r.json() if r.content else {}

    # --------------------------------------------------------------- profile
    def get_profile(self) -> dict:
        """messagesTotal + current historyId (used by bootstrap and diagnostics)."""
        return self._get("/profile")

    # --------------------------------------------------------------- labels
    def _carregar_labels(self) -> dict[str, str]:
        if self._labels_cache is None:
            resp = self._get("/labels")
            self._labels_cache = {l["name"]: l["id"] for l in resp.get("labels", [])}
        return self._labels_cache

    def user_label_names(self) -> list[str]:
        """Names of the account's own labels (type=user) — excludes Gmail
        system labels (INBOX, CATEGORY_*, ...). Used to feed the classifier the
        account's REAL labels, so it isn't limited to categorias.yaml."""
        resp = self._get("/labels")
        return [l["name"] for l in resp.get("labels", [])
                if l.get("type") == "user"]

    def garantir_label(self, nome: str) -> str:
        """Return the label id, creating the label if missing ('/' nests)."""
        cache = self._carregar_labels()
        if nome in cache:
            return cache[nome]
        criada = self._post("/labels", {
            "name": nome,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        })
        cache[nome] = criada["id"]
        return criada["id"]

    # ---------------------------------------------------- sync
    def history_added(self, start_history_id: str) -> tuple[list[dict], str | None]:
        """IDs of messages ADDED since start_history_id.

        Returns (list of {'id','threadId'}, new_history_id).
        Raises HistoryExpirada (404) when the cursor is too old — the caller
        falls back to messages.list (see motor).
        historyTypes=messageAdded avoids reprocessing Polaris' own actions.
        """
        msgs: dict[str, dict] = {}
        novo_hid: str | None = None
        page_token = None
        while True:
            params = {"startHistoryId": start_history_id,
                      "historyTypes": "messageAdded"}
            if page_token:
                params["pageToken"] = page_token
            resp = self._get("/history", params, allow_404=True)
            novo_hid = resp.get("historyId", novo_hid)
            for h in resp.get("history", []):
                for ma in h.get("messagesAdded", []):
                    m = ma["message"]
                    # ignore drafts/sent and the Trash itself
                    labels = m.get("labelIds", [])
                    if "DRAFT" in labels or "SENT" in labels or "TRASH" in labels:
                        continue
                    msgs[m["id"]] = {"id": m["id"], "threadId": m["threadId"]}
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return list(msgs.values()), novo_hid

    def messages_list(self, query: str, max_results: int | None = None) -> list[dict]:
        """Paginated messages.list. Returns [{'id','threadId'}]. Gmail-style query."""
        out: list[dict] = []
        page_token = None
        while True:
            params = {"q": query, "maxResults": 500}
            if page_token:
                params["pageToken"] = page_token
            resp = self._get("/messages", params)
            out.extend(resp.get("messages", []) or [])
            if max_results and len(out) >= max_results:
                return out[:max_results]
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return out

    # ----------------------------------------------------- message reads
    def get_meta(self, msg_id: str) -> dict:
        """Sender/subject only (format=metadata) — cheap for bulk scans
        (e.g. category suggestions) without downloading the body."""
        raw = self._get(f"/messages/{msg_id}", {
            "format": "metadata", "metadataHeaders": ["From", "Subject"]})
        headers = {h["name"].lower(): h["value"]
                   for h in raw.get("payload", {}).get("headers", [])}
        return {"id": msg_id,
                "remetente": headers.get("from", ""),
                "assunto": headers.get("subject", "")}

    def get_email(self, msg_id: str) -> EmailMsg:
        raw = self._get(f"/messages/{msg_id}", {"format": "full"})
        return self._parse_email(raw)

    def contar_mensagens_thread(self, thread_id: str) -> int:
        """Message count in the thread (rule: archive/trash only single-message threads)."""
        t = self._get(f"/threads/{thread_id}", {"format": "minimal"})
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
        """Extract text from text/plain (preferred) or text/html, recursing into multipart."""
        def decode(data: str) -> str:
            return base64.urlsafe_b64decode(data.encode()).decode("utf-8", "replace")

        mime = payload.get("mimeType", "")
        body = payload.get("body", {})
        if mime == "text/plain" and body.get("data"):
            return decode(body["data"])[:limite]
        if mime.startswith("multipart/"):
            # try plain before html
            partes = payload.get("parts", [])
            for alvo in ("text/plain", "text/html"):
                for p in partes:
                    if p.get("mimeType") == alvo and p.get("body", {}).get("data"):
                        return decode(p["body"]["data"])[:limite]
            # nested multipart
            for p in partes:
                texto = GmailClient._extrair_corpo(p, limite)
                if texto:
                    return texto
        if mime == "text/html" and body.get("data"):
            return decode(body["data"])[:limite]
        return ""

    # -------------------------------------------------------- mutations
    def modificar(
        self, msg_id: str, add: list[str] | None = None, remove: list[str] | None = None
    ) -> None:
        self._post(f"/messages/{msg_id}/modify",
                   {"addLabelIds": add or [], "removeLabelIds": remove or []})

    def trash(self, msg_id: str) -> None:
        """Send to Trash (recoverable ~30 days). NEVER a permanent delete."""
        self._post(f"/messages/{msg_id}/trash")
