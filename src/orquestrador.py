"""Orchestrator — the Polaris brain (CLI, decision, apply, state).

Flow: fetch (incremental|full) → pre-filter → classify → decide action per the
thresholds → apply on Gmail (label / archive / trash|shadow) → log.

Built-in safety guarantees:
- conservative thresholds (Review<0.70, archive≥0.80, delete≥0.95);
- deletion only for eligible categories, WITH List-Unsubscribe and single thread;
- MODO_SOMBRA_EXCLUSAO: instead of trashing, applies 'Polaris/Lixeira-candidata';
- idempotency via the 'Polaris/Processado' label;
- --dry-run never touches Gmail;
- flock against concurrent runs; state.json written atomically;
- LLM endpoint unavailable → skip the run (exit 0), no crash.

CLI (flags kept in Portuguese as a config contract):
  python -m src.orquestrador --login                 (1st OAuth, outside the container)
  python -m src.orquestrador --modo incremental [--dry-run] [--max N]
  python -m src.orquestrador --modo completo   [--dry-run] [--reprocessar] [--max N]
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import logging
import os
import shutil
import sys
from dataclasses import dataclass

from .classificador import (Catalog, Classification, load_catalog,
                            load_prompt, classify, seed_prompt_yaml)
from .gmail_client import EmailMsg, GmailClient, HistoryExpired
from .llm_client import LLMClient, LLMUnavailable
from . import prefiltro

# --- Approved thresholds ----------------------------------------------------
THRESHOLD_REVIEW = 0.70   # below this → Review
THRESHOLD_ARCHIVE = 0.80  # ≥ this and arquivar:true → remove from INBOX
THRESHOLD_DELETE = 0.95   # ≥ this (+ other criteria) → trash/shadow

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
CONFIG_DIR = os.path.join(BASE_DIR, "config")
LOGS_DIR = os.path.join(BASE_DIR, "logs")
# credentials.json is SHARED; token/categorias/state are per-account.
CATEGORIES_EXAMPLE = os.path.join(CONFIG_DIR, "categorias.yaml.example")
LOCK_PATH = os.path.join(LOGS_DIR, ".polaris.lock")
DEFAULT_ACCOUNT = "principal"   # used with --login when --conta is omitted

log = logging.getLogger("polaris")


# ---------------------------------------------------------------- accounts
def account_dir_for(account: str) -> str:
    """Config directory of an account: config/<account>/ (token/categorias/state)."""
    return os.path.join(CONFIG_DIR, account)


def configured_profiles() -> list[str]:
    """Accounts already logged in = config/ subfolders that have token.json."""
    if not os.path.isdir(CONFIG_DIR):
        return []
    return sorted(
        d for d in os.listdir(CONFIG_DIR)
        if os.path.isdir(os.path.join(CONFIG_DIR, d))
        and os.path.exists(os.path.join(CONFIG_DIR, d, "token.json"))
    )


ENV_PATH = os.path.join(BASE_DIR, ".env")


def _load_dotenv(path: str = ENV_PATH) -> None:
    """Load .env into os.environ (local run, outside Docker).

    Minimal parser (no external dependency): ignores blank lines and comments,
    accepts `KEY=VALUE` (with optional `export ` and quotes). Variables already
    present in the environment take precedence (not overwritten) — so the compose
    env_file and the systemd EnvironmentFile keep winning.
    """
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "sim", "on")


# ------------------------------------------------------------------ decision
@dataclass
class Plan:
    add_labels: list[str]        # label names to add (includes Processado)
    remove_inbox: bool           # archive
    deletion: str | None         # None | "trash" | "sombra"
    action: str                    # human action tag (pt values): revisar|label|arquivar|excluir|sombra


def decide(
    email: EmailMsg,
    cls: Classification,
    cat: Catalog,
    shadow_mode: bool,
    count_thread,   # callable() -> int (lazy: only called for archive/delete)
) -> Plan:
    """Translate the classification into concrete actions per the thresholds."""
    add = [cat.label_processed]

    # Invalid JSON OR low confidence -> Review (never archives, never deletes).
    if cls.invalid or cls.confidence < THRESHOLD_REVIEW:
        add.append(cat.review)
        return Plan(add, remove_inbox=False, deletion=None, action="revisar")

    # Confident enough -> apply the category label.
    if cls.category != cat.label_processed:
        add.append(cls.category)

    # Deletion candidate? (takes precedence over archive-only)
    wants_delete = (
        cls.delete
        and cat.eligible_delete(cls.category)
        and cls.confidence >= THRESHOLD_DELETE
        and email.has_list_unsubscribe          # mandatory deterministic signal
    )
    if wants_delete and count_thread() == 1:   # single-message threads only
        if shadow_mode:
            add.append(cat.label_trash_candidate)
            return Plan(add, remove_inbox=False, deletion="sombra", action="sombra")
        return Plan(add, remove_inbox=True, deletion="trash", action="excluir")

    # Archive candidate? (sensitive categories may veto auto-archiving)
    if (cls.archive and cls.confidence >= THRESHOLD_ARCHIVE
            and cat.eligible_archive(cls.category)
            and count_thread() == 1):
        return Plan(add, remove_inbox=True, deletion=None, action="arquivar")

    # Otherwise: category label only.
    return Plan(add, remove_inbox=False, deletion=None, action="label")


# ------------------------------------------------------------ orchestrator
class Orchestrator:
    def __init__(self, account: str, dry_run: bool, reprocessar: bool, max_n: int | None):
        self.account = account
        self.dry_run = dry_run
        self.reprocessar = reprocessar
        self.max_n = max_n
        cdir = account_dir_for(account)
        self.categorias_path = os.path.join(cdir, "categorias.yaml")
        self.state_path = os.path.join(cdir, "state.json")
        self.decisoes_path = os.path.join(LOGS_DIR, account, "decisoes.jsonl")
        self.cat = load_catalog(self.categorias_path)
        self.prompts = load_prompt(os.path.join(cdir, "prompt.yaml"))
        self.gmail = GmailClient(token_path=os.path.join(cdir, "token.json"))
        self.llm = LLMClient()
        self._label_id_cache: dict[str, str] = {}
        self.stats = {"vistos": 0, "processados": 0, "pulados": 0,
                      "revisar": 0, "arquivar": 0, "excluir": 0, "sombra": 0, "label": 0}

    # ---- state (atomic read/write) ----
    def _load_state(self) -> dict:
        if os.path.exists(self.state_path):
            with open(self.state_path, encoding="utf-8") as f:
                return json.load(f)
        return {"historyId": None, "ultima_execucao": None}

    def _save_state(self, state: dict) -> None:
        if self.dry_run:
            return
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_path)

    # ---- label name -> id (created on demand) ----
    def _label_id(self, nome: str) -> str:
        if nome not in self._label_id_cache:
            self._label_id_cache[nome] = self.gmail.ensure_label(nome)
        return self._label_id_cache[nome]

    # ---- main message loop ----
    def _process(self, pairs: list[dict]) -> None:
        processado_id = None if self.dry_run else self._label_id(self.cat.label_processed)
        for par in pairs:
            if self.max_n and self.stats["processados"] >= self.max_n:
                log.info("--max %s reached; stopping.", self.max_n)
                break
            self.stats["vistos"] += 1
            email = self.gmail.get_email(par["id"])

            # idempotency: skip anything already marked Polaris/Processado (unless --reprocessar)
            if not self.reprocessar and processado_id and processado_id in email.label_ids:
                self.stats["pulados"] += 1
                continue

            pf = prefiltro.apply(email)
            if pf.skip_llm and pf.category:
                cls = Classification(pf.category, False, False, pf.confidence, pf.reason)
            else:
                cls = classify(email, self.cat, self.llm, self.prompts)

            counter = _memo(lambda: self.gmail.count_thread_messages(email.thread_id))
            plan = decide(email, cls, self.cat, SHADOW_MODE, counter)

            self._apply(email, plan)
            self._log_record(email, cls, plan)
            self.stats["processados"] += 1
            self.stats[plan.action] = self.stats.get(plan.action, 0) + 1

    def _apply(self, email: EmailMsg, plan: Plan) -> None:
        if self.dry_run:
            return
        add_ids = [self._label_id(n) for n in plan.add_labels]
        remove_ids = ["INBOX"] if plan.remove_inbox else []
        if plan.deletion == "trash":
            # apply labels first (audit trail), then send to Trash
            self.gmail.modify(email.id, add=add_ids, remove=remove_ids)
            self.gmail.trash(email.id)
        else:
            self.gmail.modify(email.id, add=add_ids, remove=remove_ids)

    def _log_record(self, email: EmailMsg, cls: Classification, plan: Plan) -> None:
        record = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "id": email.id,
            "thread": email.thread_id,
            "remetente": email.sender,
            "assunto": email.subject,
            "categoria": cls.category,
            "confianca": cls.confidence,
            "arquivar": cls.archive,
            "excluir": cls.delete,
            "motivo": cls.reason,
            "acao": plan.action,
            "dry_run": self.dry_run,
        }
        if self.dry_run:
            log.info("[DRY] %s -> %s [cat=%s conf=%.2f delete=%s unsub=%s] %s",
                     (email.subject or "(no subject)")[:50], plan.action,
                     cls.category, cls.confidence, cls.delete,
                     email.has_list_unsubscribe, cls.reason)
        else:
            os.makedirs(os.path.dirname(self.decisoes_path), exist_ok=True)
            with open(self.decisoes_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ---- modos ----
    def incremental(self) -> None:
        state = self._load_state()
        if not state.get("historyId"):
            # Bootstrap: pin the current cursor; the backlog is for --modo completo.
            profile = self.gmail.get_profile()
            state["historyId"] = profile["historyId"]
            state["ultima_execucao"] = _now_iso()
            self._save_state(state)
            log.info("Bootstrap: historyId cursor pinned (%s). Nothing to process. "
                     "Run --modo completo for the backlog.", profile["historyId"])
            return
        try:
            pairs, new_hid = self.gmail.history_added(state["historyId"])
            log.info("Incremental: %d new message(s) since the last cursor.", len(pairs))
        except HistoryExpired:
            # Cursor too old: fall back by date + recompute the cursor.
            after_q = _after_query(state.get("ultima_execucao"))
            log.warning("historyId expired; fallback messages.list %s", after_q)
            pairs = self.gmail.messages_list(after_q)
            new_hid = self.gmail.get_profile()["historyId"]
        self._process(pairs)
        state["historyId"] = new_hid or state["historyId"]
        state["ultima_execucao"] = _now_iso()
        self._save_state(state)

    def full(self) -> None:
        query = "-in:chats"
        if not self.reprocessar:
            query += f' -label:"{self.cat.label_processed}"'
        pairs = self.gmail.messages_list(query, max_results=self.max_n)
        log.info("Full: %d candidate message(s) (query: %s).", len(pairs), query)
        self._process(pairs)
        state = self._load_state()
        state["historyId"] = self.gmail.get_profile()["historyId"]
        state["ultima_execucao"] = _now_iso()
        self._save_state(state)

    def _prune_logs(self, retention_days: int) -> None:
        """Drop decisoes.jsonl entries (of this account) older than the retention."""
        if not os.path.exists(self.decisoes_path):
            return
        limit = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=retention_days)
        keep = []
        with open(self.decisoes_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ts = dt.datetime.fromisoformat(json.loads(line)["ts"])
                    if ts >= limit:
                        keep.append(line)
                except (json.JSONDecodeError, KeyError, ValueError):
                    keep.append(line)  # keep whatever cannot be dated
        tmp = self.decisoes_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(keep) + ("\n" if keep else ""))
        os.replace(tmp, self.decisoes_path)

    def summary(self) -> None:
        s = self.stats
        log.info("Account '%s' — summary: seen=%d processed=%d skipped=%d | "
                 "label=%d archive=%d review=%d delete=%d shadow=%d%s",
                 self.account, s["vistos"], s["processados"], s["pulados"], s["label"],
                 s["arquivar"], s["revisar"], s["excluir"], s["sombra"],
                 "  (DRY-RUN: nothing applied)" if self.dry_run else "")


# ------------------------------------------------------------------ util
def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _after_query(ultima_execucao: str | None) -> str:
    """messages.list query since the last run (with a 1-day margin)."""
    if ultima_execucao:
        try:
            base = dt.datetime.fromisoformat(ultima_execucao)
        except ValueError:
            base = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)
    else:
        base = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)
    base -= dt.timedelta(days=1)  # margin so boundary messages are not missed
    return f"after:{base.strftime('%Y/%m/%d')} -in:chats"


def _memo(fn):
    """Memoize a zero-arg callable (evaluated at most once)."""
    cache = {}
    def wrapped():
        if "v" not in cache:
            cache["v"] = fn()
        return cache["v"]
    return wrapped


def _setup_logging() -> None:
    os.makedirs(LOGS_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    log.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    fh = logging.FileHandler(os.path.join(LOGS_DIR, "execucao.log"), encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)


# globals read from the environment (set in main after loading .env)
SHADOW_MODE = True


def _onboarding(account: str) -> int:
    """Add an account: do the OAuth login and get everything ready in one command.

    Keeps the flow simple so first-time users are not scared off: creates
    config/<account>/, generates the token, seeds an initial categorias.yaml and
    prints the next step.
    """
    cdir = account_dir_for(account)
    os.makedirs(cdir, exist_ok=True)
    token_path = os.path.join(cdir, "token.json")
    try:
        GmailClient.authenticate_interactive(token_path=token_path)
    except FileNotFoundError as e:
        log.error("%s", e)
        return 1
    cat_path = os.path.join(cdir, "categorias.yaml")
    seeded = False
    if not os.path.exists(cat_path) and os.path.exists(CATEGORIES_EXAMPLE):
        shutil.copy(CATEGORIES_EXAMPLE, cat_path)
        seeded = True
    prompt_path = os.path.join(cdir, "prompt.yaml")
    if not os.path.exists(prompt_path):
        seed_prompt_yaml(prompt_path)
    log.info("")
    log.info("✅ Account '%s' added! Token at %s", account, token_path)
    if seeded:
        log.info("   Seeded an initial categorias.yaml: %s", cat_path)
        log.info("   -> Edit it with YOUR own Gmail categories/labels (plain text).")
    log.info("   Preview the triage WITHOUT applying anything:")
    log.info("       python -m src.orquestrador --account %s --modo completo --dry-run --max 30",
             account)
    return 0


def _run_account(account: str, args, retention: int) -> int:
    """Run the triage for ONE account. Returns 0 on success, 1 if it failed to start."""
    log.info("──── Account '%s' ────", account)
    try:
        orq = Orchestrator(account, args.dry_run, args.reprocessar, args.max_n)
    except Exception as e:  # missing token/categorias etc.
        log.error("Account '%s': failed to initialize: %s", account, e)
        return 1
    if not args.dry_run:
        orq._prune_logs(retention)
    try:
        if args.modo == "incremental":
            orq.incremental()
        else:
            orq.full()
    except LLMUnavailable as e:
        log.warning("Account '%s': LLM went down mid-run (%s). Continuing without "
                    "error; the incremental run catches up next time.", account, e)
    finally:
        orq.summary()
    return 0


def _suggest_categories(account: str, args) -> int:
    """Category suggestion flow.

    --sugerir-categorias: sample the mailbox -> LLM proposes -> interactive
    terminal accepts on the spot; without a TTY, saves logs/<account>/sugestoes.json
    and exits (a front-end reads that JSON and sends the acceptance via --aceitar).
    --aceitar '1,3'|'todas': apply saved suggestions to categorias.yaml.
    """
    from . import sugestor

    cdir = account_dir_for(account)
    categorias_path = os.path.join(cdir, "categorias.yaml")
    if not os.path.exists(os.path.join(cdir, "token.json")):
        log.error("Account '%s' not logged in. Run: python -m src.orquestrador "
                  "--account %s --login", account, account)
        return 1

    # ---- accept only (front-end / second step) ----
    if args.aceitar and not args.sugerir:
        try:
            sugestoes = sugestor.load_json(account, LOGS_DIR)
        except FileNotFoundError as e:
            log.error("%s", e)
            return 1
        if args.aceitar.strip().lower() in ("todas", "todos", "all"):
            accepted = sugestoes
        else:
            idx = [int(t) for t in args.aceitar.split(",") if t.strip().isdigit()]
            accepted = [sugestoes[i - 1] for i in idx if 1 <= i <= len(sugestoes)]
        if not accepted:
            log.info("Nothing to accept.")
            return 0
        sugestor.apply_accepts(categorias_path, accepted)
        log.info("✅ %d category(ies) added to %s: %s",
                 len(accepted), categorias_path,
                 ", ".join(a["nome"] for a in accepted))
        return 0

    # ---- scan + suggestion ----
    try:
        cat = load_catalog(categorias_path)
        gmail = GmailClient(token_path=os.path.join(cdir, "token.json"))
        llm = LLMClient()
    except Exception as e:
        log.error("Account '%s': failed to initialize: %s", account, e)
        return 1
    if not llm.available():
        log.error("LLM endpoint unavailable (%s). Start the model and run again.",
                  llm.base_url)
        return 1

    max_n = args.max_n or 200
    log.info("Analyzing %d emails of account '%s' (sender/subject only)...",
             max_n, account)
    metas = sugestor.sample(gmail, max_n)
    try:
        sugestoes = sugestor.suggest(metas, cat, llm, log=log)
    except LLMUnavailable as e:
        log.error("LLM went down during the analysis (%s). Run again.", e)
        return 1
    if not sugestoes:
        log.info("No new category to suggest — the current ones already cover the mailbox.")
        return 0

    path = sugestor.save_json(account, LOGS_DIR, sugestoes)
    if sugestor.interativo():
        accepted = sugestor._checkbox_prompt(sugestoes)
        if not accepted:
            log.info("None accepted. Suggestions saved to %s "
                     "(accept later with --aceitar).", path)
            return 0
        sugestor.apply_accepts(categorias_path, accepted)
        log.info("✅ %d category(ies) added: %s",
                 len(accepted), ", ".join(a["nome"] for a in accepted))
    else:
        # No TTY (front-end/automation): just publish the JSON.
        print(json.dumps({"sugestoes": sugestoes}, ensure_ascii=False, indent=2))
        log.info("Suggestions saved to %s. Accept with: --account %s --aceitar '1,2'",
                 path, account)
    return 0


def main(argv=None) -> int:
    global SHADOW_MODE
    ap = argparse.ArgumentParser(prog="polaris", description="Gmail triage with a local LLM.")
    ap.add_argument("--account", default=None,
                    help="which account to process (config/<account>/). Omitted: ALL "
                         "configured accounts (with --login: 'principal').")
    ap.add_argument("--modo", choices=["incremental", "completo"], default="incremental", dest="modo")
    ap.add_argument("--dry-run", action="store_true", help="apply nothing to Gmail")
    ap.add_argument("--reprocessar", action="store_true",
                    help="do not skip messages already marked Polaris/Processado")
    ap.add_argument("--max", type=int, default=None, dest="max_n",
                    help="limit how many messages to process")
    ap.add_argument("--login", action="store_true",
                    help="add/re-authenticate an account (OAuth login) and exit")
    ap.add_argument("--sugerir-categorias", action="store_true", dest="sugerir",
                    help="scan a sample of the account and suggest new categories "
                         "(interactive in the terminal; without a TTY saves JSON) and exit")
    ap.add_argument("--aceitar", default=None, metavar="NUMS",
                    help="accept saved suggestions (logs/<account>/sugestoes.json): "
                         "'1,3' or 'todas'. Used by the front-end / after --sugerir-categorias")
    args = ap.parse_args(argv)

    _load_dotenv()
    _setup_logging()

    if args.login:
        return _onboarding(args.conta or DEFAULT_ACCOUNT)

    if args.sugerir or args.aceitar:
        if not args.conta:
            log.error("--sugerir-categorias/--aceitar require --account <name>.")
            return 1
        return _suggest_categories(args.conta, args)

    # Without --account: process ALL configured accounts (ideal for the timer).
    accounts = [args.conta] if args.conta else configured_profiles()
    if not accounts:
        log.error("No account configured. Add one with: "
                  "python -m src.orquestrador --account <name> --login")
        return 1
    # Account requested explicitly but not logged in yet -> clear message.
    missing = [c for c in accounts
                if not os.path.exists(os.path.join(account_dir_for(c), "token.json"))]
    if missing:
        log.error("Account(s) not logged in: %s. Add with: "
                  "python -m src.orquestrador --account %s --login",
                  ", ".join(missing), missing[0])
        return 1

    SHADOW_MODE = _env_bool("MODO_SOMBRA_EXCLUSAO", True)
    retention = int(os.environ.get("LOG_RETENCAO_DIAS", "90"))

    # Global lock against concurrent runs (timer vs manual run).
    os.makedirs(LOGS_DIR, exist_ok=True)
    lock_fd = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.warning("Another Polaris run is in progress (lock). Exiting.")
        return 0

    try:
        # The LLM endpoint is shared across accounts — check it once.
        try:
            llm = LLMClient()
        except ValueError as e:
            log.error("Invalid LLM config: %s", e)
            return 1
        if not llm.available():
            log.warning("LLM endpoint unavailable (%s). Skipping this run.",
                        llm.base_url)
            return 0

        rc = 0
        for account in accounts:
            rc |= _run_account(account, args, retention)
        return rc
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == "__main__":
    raise SystemExit(main())
