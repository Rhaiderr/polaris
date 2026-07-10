"""Triage engine — the Polaris orchestrator adapted to the HA integration.

Same flow and SAME safety guarantees as the standalone CLI version:
fetch (incremental|full) → pre-filter → classify → decide action per the
thresholds → apply on Gmail (label / archive / trash|shadow) → audit log.

- conservative thresholds (Review<0.70, archive≥0.80, trash≥0.95);
- trashing only for eligible categories, WITH List-Unsubscribe, single-message
  threads only;
- shadow mode: applies the 'Polaris/Lixeira-candidata' label instead of trashing;
- idempotency via the 'Polaris/Processado' label;
- dry_run never touches Gmail;
- LLM endpoint down → run is skipped (no error).

Differences from the CLI: the access token comes from Home Assistant
(OAuth2Session, refreshed by HA); config/state/audit live in
/config/polaris/<email>/; everything here is SYNCHRONOUS — the runtime calls
it through the executor.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import shutil
from dataclasses import dataclass

from .classificador import (Catalogo, Classificacao, carregar_catalogo,
                            classificar)
from .gmail_client import EmailMsg, GmailClient, HistoryExpirada
from .llm_client import LLMClient, LLMIndisponivel
from . import prefiltro

# --- Approved thresholds ------------------------------------------------------
LIMIAR_REVISAR = 0.70   # below this → Review
LIMIAR_ARQUIVAR = 0.80  # at/above this and arquivar:true → remove from INBOX
LIMIAR_EXCLUIR = 0.95   # at/above this (+ other criteria) → trash/shadow

LOG_RETENTION_DAYS = 90

_LOGGER = logging.getLogger(__name__)

CATEGORIES_EXAMPLE = os.path.join(os.path.dirname(__file__),
                                  "categorias.yaml.example")


@dataclass
class MotorConfig:
    """Everything a run needs (comes from the config entry options)."""
    account_dir: str          # /config/polaris/<email>
    llm_base_url: str
    llm_model: str
    llm_api_key: str = ""
    shadow_mode: bool = True
    dry_run: bool = False
    reprocess: bool = False
    max_n: int | None = None


def prepare_account_dir(account_dir: str) -> None:
    """Create the account directory and seed an initial categorias.yaml."""
    os.makedirs(account_dir, exist_ok=True)
    cat_path = os.path.join(account_dir, "categorias.yaml")
    if not os.path.exists(cat_path) and os.path.exists(CATEGORIES_EXAMPLE):
        shutil.copy(CATEGORIES_EXAMPLE, cat_path)
        _LOGGER.info("Seeded initial categorias.yaml at %s — edit it with "
                     "your own Gmail labels", cat_path)


def build_service(access_token: str):
    """Build the Gmail API client from the HA-managed access token."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials(token=access_token)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ------------------------------------------------------------------ decision
@dataclass
class Plano:
    add_labels: list[str]        # label names to add (includes Processado)
    remove_inbox: bool           # archive
    exclusao: str | None         # None | "trash" | "shadow"
    acao: str                    # human action tag: review|label|archive|trash|shadow


def decidir(
    email: EmailMsg,
    cls: Classificacao,
    cat: Catalogo,
    shadow_mode: bool,
    contar_thread,   # callable() -> int (lazy: only called for archive/trash)
) -> Plano:
    """Translate the classification into concrete actions per the thresholds."""
    add = [cat.label_processado]

    # Invalid JSON OR low confidence → Review (never archives, never trashes).
    if cls.invalido or cls.confianca < LIMIAR_REVISAR:
        add.append(cat.revisar)
        return Plano(add, remove_inbox=False, exclusao=None, acao="review")

    # Confident enough → apply the category label.
    if cls.categoria != cat.label_processado:
        add.append(cls.categoria)

    # Trash candidate? (takes precedence over archive-only)
    quer_excluir = (
        cls.excluir
        and cat.elegivel_exclusao(cls.categoria)
        and cls.confianca >= LIMIAR_EXCLUIR
        and email.tem_list_unsubscribe          # mandatory deterministic signal
    )
    if quer_excluir and contar_thread() == 1:   # single-message threads only
        if shadow_mode:
            add.append(cat.label_lixeira_candidata)
            return Plano(add, remove_inbox=False, exclusao="shadow", acao="shadow")
        return Plano(add, remove_inbox=True, exclusao="trash", acao="trash")

    # Archive candidate? (sensitive categories may veto auto-archiving)
    if (cls.arquivar and cls.confianca >= LIMIAR_ARQUIVAR
            and cat.elegivel_arquivamento(cls.categoria)
            and contar_thread() == 1):
        return Plano(add, remove_inbox=True, exclusao=None, acao="archive")

    # Otherwise: category label only.
    return Plano(add, remove_inbox=False, exclusao=None, acao="label")


# ------------------------------------------------------------------ engine
class Motor:
    def __init__(self, gmail: GmailClient, llm: LLMClient, cat: Catalogo,
                 cfg: MotorConfig):
        self.gmail = gmail
        self.llm = llm
        self.cat = cat
        self.cfg = cfg
        self.state_path = os.path.join(cfg.account_dir, "state.json")
        self.decisions_path = os.path.join(cfg.account_dir, "decisions.jsonl")
        self._label_id_cache: dict[str, str] = {}
        self.stats = {"seen": 0, "processed": 0, "skipped": 0,
                      "review": 0, "archive": 0, "trash": 0,
                      "shadow": 0, "label": 0}

    # ---- state (atomic read/write) ----
    def _load_state(self) -> dict:
        if os.path.exists(self.state_path):
            with open(self.state_path, encoding="utf-8") as f:
                return json.load(f)
        return {"historyId": None, "last_run": None}

    def _save_state(self, state: dict) -> None:
        if self.cfg.dry_run:
            return
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_path)

    # ---- label name -> id (created on demand) ----
    def _label_id(self, nome: str) -> str:
        if nome not in self._label_id_cache:
            self._label_id_cache[nome] = self.gmail.garantir_label(nome)
        return self._label_id_cache[nome]

    # ---- main message loop ----
    def _processar(self, pares: list[dict]) -> None:
        processado_id = (None if self.cfg.dry_run
                         else self._label_id(self.cat.label_processado))
        for par in pares:
            if self.cfg.max_n and self.stats["processed"] >= self.cfg.max_n:
                _LOGGER.info("Limit of %s messages reached; stopping.",
                             self.cfg.max_n)
                break
            self.stats["seen"] += 1
            email = self.gmail.get_email(par["id"])

            # idempotency: skip anything already labeled Polaris/Processado
            if (not self.cfg.reprocess and processado_id
                    and processado_id in email.label_ids):
                self.stats["skipped"] += 1
                continue

            pf = prefiltro.aplicar(email)
            if pf.pular_llm and pf.categoria:
                cls = Classificacao(pf.categoria, False, False,
                                    pf.confianca, pf.motivo)
            else:
                cls = classificar(email, self.cat, self.llm)

            contador = _memo(
                lambda: self.gmail.contar_mensagens_thread(email.thread_id))
            plano = decidir(email, cls, self.cat, self.cfg.shadow_mode, contador)

            self._aplicar(email, plano)
            self._logar(email, cls, plano)
            self.stats["processed"] += 1
            self.stats[plano.acao] = self.stats.get(plano.acao, 0) + 1

    def _aplicar(self, email: EmailMsg, plano: Plano) -> None:
        if self.cfg.dry_run:
            return
        add_ids = [self._label_id(n) for n in plano.add_labels]
        remove_ids = ["INBOX"] if plano.remove_inbox else []
        if plano.exclusao == "trash":
            # apply labels first (audit trail), then send to Trash
            self.gmail.modificar(email.id, add=add_ids, remove=remove_ids)
            self.gmail.trash(email.id)
        else:
            self.gmail.modificar(email.id, add=add_ids, remove=remove_ids)

    def _logar(self, email: EmailMsg, cls: Classificacao, plano: Plano) -> None:
        registro = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "id": email.id,
            "thread": email.thread_id,
            "sender": email.remetente,
            "subject": email.assunto,
            "category": cls.categoria,
            "confidence": cls.confianca,
            "archive": cls.arquivar,
            "trash": cls.excluir,
            "reason": cls.motivo,
            "action": plano.acao,
            "dry_run": self.cfg.dry_run,
        }
        if self.cfg.dry_run:
            _LOGGER.info("[DRY] %s → %s [cat=%s conf=%.2f trash=%s unsub=%s] %s",
                         (email.assunto or "(no subject)")[:50], plano.acao,
                         cls.categoria, cls.confianca, cls.excluir,
                         email.tem_list_unsubscribe, cls.motivo)
        else:
            os.makedirs(os.path.dirname(self.decisions_path), exist_ok=True)
            with open(self.decisions_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(registro, ensure_ascii=False) + "\n")

    # ---- modes ----
    def incremental(self) -> None:
        state = self._load_state()
        if not state.get("historyId"):
            # Bootstrap: pin the current cursor; the backlog is handled by
            # the 'full' mode.
            prof = self.gmail.get_profile()
            state["historyId"] = prof["historyId"]
            state["last_run"] = _now_iso()
            self._save_state(state)
            _LOGGER.info("Bootstrap: historyId cursor pinned (%s). Nothing to "
                         "process. Call the service with mode 'full' for the "
                         "backlog.", prof["historyId"])
            self.stats["bootstrap"] = True
            return
        try:
            pares, novo_hid = self.gmail.history_added(state["historyId"])
            _LOGGER.info("Incremental: %d new message(s) since the last "
                         "cursor.", len(pares))
        except HistoryExpirada:
            # Cursor too old: date-based fallback + re-pin the cursor.
            depois = _after_query(state.get("last_run"))
            _LOGGER.warning("historyId expired; falling back to messages.list "
                            "%s", depois)
            pares = self.gmail.messages_list(depois)
            novo_hid = self.gmail.get_profile()["historyId"]
        self._processar(pares)
        state["historyId"] = novo_hid or state["historyId"]
        state["last_run"] = _now_iso()
        self._save_state(state)

    def full(self) -> None:
        query = "-in:chats"
        if not self.cfg.reprocess:
            query += f' -label:"{self.cat.label_processado}"'
        pares = self.gmail.messages_list(query, max_results=self.cfg.max_n)
        _LOGGER.info("Full: %d candidate message(s) (query: %s).",
                     len(pares), query)
        self._processar(pares)
        state = self._load_state()
        state["historyId"] = self.gmail.get_profile()["historyId"]
        state["last_run"] = _now_iso()
        self._save_state(state)

    def prune_logs(self, retention_days: int = LOG_RETENTION_DAYS) -> None:
        """Drop decisions.jsonl entries older than the retention window."""
        if not os.path.exists(self.decisions_path):
            return
        limite = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=retention_days)
        manter = []
        with open(self.decisions_path, encoding="utf-8") as f:
            for linha in f:
                linha = linha.strip()
                if not linha:
                    continue
                try:
                    ts = dt.datetime.fromisoformat(json.loads(linha)["ts"])
                    if ts >= limite:
                        manter.append(linha)
                except (json.JSONDecodeError, KeyError, ValueError):
                    manter.append(linha)  # keep whatever we cannot date
        tmp = self.decisions_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(manter) + ("\n" if manter else ""))
        os.replace(tmp, self.decisions_path)


# ---------------------------------------------------------------- entry points
def executar(access_token: str, cfg: MotorConfig, mode: str) -> dict:
    """One full triage run (called through the executor). Returns stats.

    Never raises on LLM downtime: signals it via stats["skipped_reason"] /
    stats["interrupted"] — same semantics as the CLI (incremental catches up).
    """
    gmail = GmailClient(build_service(access_token))
    llm = LLMClient(base_url=cfg.llm_base_url, model=cfg.llm_model,
                    api_key=cfg.llm_api_key)
    if not llm.disponivel():
        _LOGGER.warning("LLM endpoint unavailable (%s). Skipping this run.",
                        llm.base_url)
        return {"skipped_reason": "llm_unavailable"}

    cat = carregar_catalogo(os.path.join(cfg.account_dir, "categorias.yaml"))
    motor = Motor(gmail, llm, cat, cfg)
    if not cfg.dry_run:
        motor.prune_logs()
    try:
        if mode == "full":
            motor.full()
        else:
            motor.incremental()
    except LLMIndisponivel as e:
        _LOGGER.warning("LLM went down mid-run (%s). The next incremental "
                        "run catches up.", e)
        motor.stats["interrupted"] = "llm_unavailable"
    motor.stats["last_run"] = _now_iso()
    return motor.stats


def rodar_sugestor(access_token: str, cfg: MotorConfig, max_n: int) -> list[dict]:
    """Sample the mailbox and return category suggestions (saved in account_dir)."""
    from . import sugestor

    gmail = GmailClient(build_service(access_token))
    llm = LLMClient(base_url=cfg.llm_base_url, model=cfg.llm_model,
                    api_key=cfg.llm_api_key)
    if not llm.disponivel():
        raise LLMIndisponivel(f"endpoint unavailable: {llm.base_url}")
    cat = carregar_catalogo(os.path.join(cfg.account_dir, "categorias.yaml"))
    metas = sugestor.amostrar(gmail, max_n)
    sugestoes = sugestor.sugerir(metas, cat, llm, log=_LOGGER)
    sugestor.salvar_json(cfg.account_dir, sugestoes)
    return sugestoes


def aceitar_sugestoes(account_dir: str, numbers: str) -> list[str]:
    """Apply saved suggestions ('1,3' or 'all'). Returns the added names."""
    from . import sugestor

    sugestoes = sugestor.carregar_json(account_dir)
    if numbers.strip().lower() in ("all", "todas", "todos"):
        aceitas = sugestoes
    else:
        idx = [int(t) for t in numbers.split(",") if t.strip().isdigit()]
        aceitas = [sugestoes[i - 1] for i in idx if 1 <= i <= len(sugestoes)]
    if aceitas:
        sugestor.aplicar_aceites(
            os.path.join(account_dir, "categorias.yaml"), aceitas)
    return [a["nome"] for a in aceitas]


# ------------------------------------------------------------------ helpers
def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _after_query(last_run: str | None) -> str:
    """messages.list query starting at the last run (with 1 day of slack)."""
    if last_run:
        try:
            base = dt.datetime.fromisoformat(last_run)
        except ValueError:
            base = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)
    else:
        base = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)
    base -= dt.timedelta(days=1)  # slack so boundary messages are not missed
    return f"after:{base.strftime('%Y/%m/%d')} -in:chats"


def _memo(fn):
    """Memoize a zero-arg callable (evaluated at most once)."""
    cache = {}

    def wrapped():
        if "v" not in cache:
            cache["v"] = fn()
        return cache["v"]
    return wrapped
