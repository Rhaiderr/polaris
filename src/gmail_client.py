"""Gmail client — gmail.modify scope only (label + archive + trash).

Handles all conversation with the Gmail API:
- OAuth (loads credentials/token; 1st login generates token.json outside the container);
- robust incremental sync (History API) with bootstrap via getProfile,
  automatic fallback when the historyId expires (404) and historyTypes=messageAdded
  (anti-feedback: Polaris' own actions are not reprocessed);
- full sync (paginated messages.list);
- batch label application (batchModify), archiving and trash;
- extraction of the relevant content of each message (headers + body).

Never does a permanent delete: deletion is always users.messages.trash.
"""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field

# NB: the Google libraries are imported LAZILY (inside the methods that use
# them). So importing this module just for EmailMsg/HistoryExpired
# (e.g. in the offline sanity check tests/dry_run.py) does not need the package.

# Minimal scope: read + modify (label/archive/trash). NO delete, NO send.
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config")
# credentials.json is SHARED across accounts: one Desktop OAuth app authorizes
# as many Google accounts as you want. token.json is per-account (config/<account>/).
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
        """1st OAuth login of an account (runs OUTSIDE the container, once).

        Writes the token to `token_path` (config/<account>/token.json). Requires
        the SHARED credentials.json (see docs/gmail-credentials.md).
        Starts a local server on a FIXED port (env OAUTH_PORT, default 8765) and
        does NOT try to open a browser — it prints the consent URL. This works on a
        headless machine / over SSH: just forward the port
        (ssh -L PORT:localhost:PORT ...) and open the URL in your machine's browser.
        """
        from google_auth_oauthlib.flow import InstalledAppFlow
        if not os.path.exists(credentials_path):
            raise FileNotFoundError(
                f"Missing {credentials_path}. Follow docs/gmail-credentials.md."
            )
        os.makedirs(os.path.dirname(token_path), exist_ok=True)
        port = int(os.environ.get("OAUTH_PORT", "8765"))
        flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
        creds = flow.run_local_server(
            port=port,
            open_browser=False,
            authorization_prompt_message=(
                "Open this URL in your machine's browser "
                f"(with an SSH tunnel: ssh -L {port}:localhost:{port} YOUR_HOST):\n{{url}}"
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
                    "No valid OAuth token. Run the 1st login: "
                    "python -m src.orquestrador --account <name> --login  (outside the container)."
                )
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    # --------------------------------------------------------------- profile
    def get_profile(self) -> dict:
        """messagesTotal + current historyId (used in bootstrap and diagnostics)."""
        return self.service.users().getProfile(userId="me").execute()

    # --------------------------------------------------------------- labels
    def _load_labels(self) -> dict[str, str]:
        if self._labels_cache is None:
            resp = self.service.users().labels().list(userId="me").execute()
            self._labels_cache = {l["name"]: l["id"] for l in resp.get("labels", [])}
        return self._labels_cache

    def ensure_label(self, nome: str) -> str:
        """Return the label id, creating it if missing (nesting via '/')."""
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

    # ---------------------------------------------------- sync
    def history_added(self, start_history_id: str) -> tuple[list[dict], str | None]:
        """IDs of messages ADDED since start_history_id.

        Returns (list of {'id','threadId'}, new_history_id).
        Raises HistoryExpired (404) when the cursor is too old — the
        caller falls back to messages.list (see orquestrador).
        historyTypes=messageAdded avoids reprocessing Polaris' own actions.
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
                        # ignore drafts/sent and Polaris' own Trash
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
        """Paginated messages.list. Returns [{'id','threadId'}]. Gmail-style query."""
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

    # ----------------------------------------------------- message read
    def get_meta(self, msg_id: str) -> dict:
        """Sender/subject only (format=metadata) — cheap for bulk scans
        (e.g. category suggestion), without downloading the body."""
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
        """Number of messages in the thread (rule: archive/trash only on single thread)."""
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
        """Extract text from text/plain (preferred) or text/html, recursive in multipart."""
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

    # -------------------------------------------------------- modifications
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
        """Apply the SAME labels to several ids at once (saves calls)."""
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
        """Send to Trash (recoverable ~30 days). NEVER permanent delete."""
        self.service.users().messages().trash(userId="me", id=msg_id).execute()


class HistoryExpired(Exception):
    """startHistoryId too old (Gmail returned 404). Caller uses a fallback."""
