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

from .classificador import (Catalog, Classification, load_catalog,
                            load_prompt, classify, seed_prompt_yaml)
from .gmail_client import EmailMsg, GmailClient, HistoryExpired
from .llm_client import LLMClient, LLMUnavailable
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
class EngineConfig:
    """Everything a run needs (comes from the config entry options)."""
    account_dir: str          # /config/polaris/<email>
    llm_base_url: str
    llm_model: str
    llm_api_key: str = ""
    shadow_mode: bool = True
    dry_run: bool = False
    reprocess: bool = False
    max_n: int | None = None
    usar_labels_gmail: bool = True   # merge the account's real Gmail labels as categories
    report_dir: str = ""      # /config/www/polaris (for the servable HTML report)
    report_token: str = ""    # unguessable filename component for the report
    webhook_url: str = ""     # /api/webhook/<id> for one-click accept from the report


def prepare_account_dir(account_dir: str) -> None:
    """Create the account directory and seed an initial categorias.yaml."""
    os.makedirs(account_dir, exist_ok=True)
    cat_path = os.path.join(account_dir, "categorias.yaml")
    if not os.path.exists(cat_path) and os.path.exists(CATEGORIES_EXAMPLE):
        shutil.copy(CATEGORIES_EXAMPLE, cat_path)
        _LOGGER.info("Seeded initial categorias.yaml at %s — edit it with "
                     "your own Gmail labels", cat_path)
    prompt_path = os.path.join(account_dir, "prompt.yaml")
    if not os.path.exists(prompt_path):
        seed_prompt_yaml(prompt_path)
        _LOGGER.info("Seeded editable prompt.yaml at %s — tweak it to regulate "
                     "the model for your mailbox", prompt_path)


# ------------------------------------------------------------------ decision
@dataclass
class Plan:
    add_labels: list[str]        # label names to add (includes Processado)
    remove_inbox: bool           # archive
    deletion: str | None         # None | "trash" | "shadow"
    action: str                    # human action tag: review|label|archive|trash|shadow


def decide(
    email: EmailMsg,
    cls: Classification,
    cat: Catalog,
    shadow_mode: bool,
    count_thread,   # callable() -> int (lazy: only called for archive/trash)
) -> Plan:
    """Translate the classification into concrete actions per the thresholds."""
    add = [cat.label_processed]

    # Invalid JSON OR low confidence → Review (never archives, never trashes).
    if cls.invalid or cls.confidence < LIMIAR_REVISAR:
        add.append(cat.review)
        return Plan(add, remove_inbox=False, deletion=None, action="review")

    # Confident enough → apply the category label.
    if cls.category != cat.label_processed:
        add.append(cls.category)

    # Trash candidate? (takes precedence over archive-only)
    wants_delete = (
        cls.delete
        and cat.eligible_delete(cls.category)
        and cls.confidence >= LIMIAR_EXCLUIR
        and email.has_list_unsubscribe          # mandatory deterministic signal
    )
    if wants_delete and count_thread() == 1:   # single-message threads only
        if shadow_mode:
            add.append(cat.label_trash_candidate)
            return Plan(add, remove_inbox=False, deletion="shadow", action="shadow")
        return Plan(add, remove_inbox=True, deletion="trash", action="trash")

    # Archive candidate? (sensitive categories may veto auto-archiving)
    if (cls.archive and cls.confidence >= LIMIAR_ARQUIVAR
            and cat.eligible_archive(cls.category)
            and count_thread() == 1):
        return Plan(add, remove_inbox=True, deletion=None, action="archive")

    # Otherwise: category label only.
    return Plan(add, remove_inbox=False, deletion=None, action="label")


# ------------------------------------------------------------------ engine
class Engine:
    def __init__(self, gmail: GmailClient, llm: LLMClient, cat: Catalog,
                 cfg: EngineConfig):
        self.gmail = gmail
        self.llm = llm
        self.cat = cat
        self.cfg = cfg
        self.prompts = load_prompt(
            os.path.join(cfg.account_dir, "prompt.yaml"))
        self.state_path = os.path.join(cfg.account_dir, "state.json")
        self.decisions_path = os.path.join(cfg.account_dir, "decisions.jsonl")
        self._label_id_cache: dict[str, str] = {}
        self.records: list[dict] = []   # accumulated for the per-run report
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
            self._label_id_cache[nome] = self.gmail.ensure_label(nome)
        return self._label_id_cache[nome]

    # ---- main message loop ----
    def _process(self, pairs: list[dict]) -> None:
        processado_id = (None if self.cfg.dry_run
                         else self._label_id(self.cat.label_processed))
        for par in pairs:
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

            pf = prefiltro.apply(email)
            if pf.skip_llm and pf.category:
                cls = Classification(pf.category, False, False,
                                    pf.confidence, pf.reason)
            else:
                cls = classify(email, self.cat, self.llm, self.prompts)

            contador = _memo(
                lambda: self.gmail.count_thread_messages(email.thread_id))
            plano = decide(email, cls, self.cat, self.cfg.shadow_mode, contador)

            self._apply(email, plano)
            self._log_record(email, cls, plano)
            self.stats["processed"] += 1
            self.stats[plano.action] = self.stats.get(plano.action, 0) + 1

    def _apply(self, email: EmailMsg, plano: Plan) -> None:
        if self.cfg.dry_run:
            return
        add_ids = [self._label_id(n) for n in plano.add_labels]
        remove_ids = ["INBOX"] if plano.remove_inbox else []
        if plano.deletion == "trash":
            # apply labels first (audit trail), then send to Trash
            self.gmail.modify(email.id, add=add_ids, remove=remove_ids)
            self.gmail.trash(email.id)
        else:
            self.gmail.modify(email.id, add=add_ids, remove=remove_ids)

    def _log_record(self, email: EmailMsg, cls: Classification, plano: Plan) -> None:
        record = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "id": email.id,
            "thread": email.thread_id,
            "sender": email.sender,
            "subject": email.subject,
            "category": cls.category,
            "confidence": cls.confidence,
            "archive": cls.archive,
            "trash": cls.delete,
            "reason": cls.reason,
            "action": plano.action,
            "destino": _target(plano, self.cat),
            "dry_run": self.cfg.dry_run,
        }
        self.records.append(record)   # always, for the per-run report
        if self.cfg.dry_run:
            _LOGGER.info("[DRY] %s → %s [cat=%s conf=%.2f trash=%s unsub=%s] %s",
                         (email.subject or "(no subject)")[:50], plano.action,
                         cls.category, cls.confidence, cls.delete,
                         email.has_list_unsubscribe, cls.reason)
        else:
            os.makedirs(os.path.dirname(self.decisions_path), exist_ok=True)
            with open(self.decisions_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ---- modes ----
    def incremental(self) -> None:
        state = self._load_state()
        if not state.get("historyId"):
            # Bootstrap: pin the current cursor; the backlog is handled by
            # the 'full' mode.
            profile = self.gmail.get_profile()
            state["historyId"] = profile["historyId"]
            state["last_run"] = _now_iso()
            self._save_state(state)
            _LOGGER.info("Bootstrap: historyId cursor pinned (%s). Nothing to "
                         "process. Call the service with mode 'full' for the "
                         "backlog.", profile["historyId"])
            self.stats["bootstrap"] = True
            return
        try:
            pairs, novo_hid = self.gmail.history_added(state["historyId"])
            _LOGGER.info("Incremental: %d new message(s) since the last "
                         "cursor.", len(pairs))
        except HistoryExpired:
            # Cursor too old: date-based fallback + re-pin the cursor.
            depois = _after_query(state.get("last_run"))
            _LOGGER.warning("historyId expired; falling back to messages.list "
                            "%s", depois)
            pairs = self.gmail.messages_list(depois)
            novo_hid = self.gmail.get_profile()["historyId"]
        self._process(pairs)
        state["historyId"] = novo_hid or state["historyId"]
        state["last_run"] = _now_iso()
        self._save_state(state)

    def full(self) -> None:
        query = "-in:chats"
        if not self.cfg.reprocess:
            query += f' -label:"{self.cat.label_processed}"'
        pairs = self.gmail.messages_list(query, max_results=self.cfg.max_n)
        _LOGGER.info("Full: %d candidate message(s) (query: %s).",
                     len(pairs), query)
        self._process(pairs)
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
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ts = dt.datetime.fromisoformat(json.loads(line)["ts"])
                    if ts >= limite:
                        manter.append(line)
                except (json.JSONDecodeError, KeyError, ValueError):
                    manter.append(line)  # keep whatever we cannot date
        tmp = self.decisions_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(manter) + ("\n" if manter else ""))
        os.replace(tmp, self.decisions_path)


# ---------------------------------------------------------------- entry points
def _catalog(cfg: EngineConfig, gmail: GmailClient) -> Catalog:
    """Catalog = categorias.yaml + (optionally) the account's real Gmail labels.

    Merged labels get safe defaults (never trash-eligible, archivable), so the
    classifier can target ANY existing label without hand-editing the YAML —
    the project isn't limited to what's declared. categorias.yaml still wins
    for descriptions and the exclusion/archive flags.
    """
    cat = load_catalog(os.path.join(cfg.account_dir, "categorias.yaml"))
    if not cfg.usar_labels_gmail:
        return cat
    internal = {cat.label_processed, cat.label_trash_candidate, cat.review}
    try:
        for nome in gmail.user_label_names():
            if (nome not in cat.names and nome not in internal
                    and not nome.startswith("Polaris/")):
                cat.names.append(nome)   # safe defaults via elegivel_* getters
    except Exception:  # noqa: BLE001 — label discovery is best-effort
        _LOGGER.exception("Could not list Gmail labels; using categorias.yaml only")
    return cat


def run_triage(access_token: str, cfg: EngineConfig, mode: str) -> dict:
    """One full triage run (called through the executor). Returns stats.

    Never raises on LLM downtime: signals it via stats["skipped_reason"] /
    stats["interrupted"] — same semantics as the CLI (incremental catches up).
    """
    gmail = GmailClient(access_token)
    llm = LLMClient(base_url=cfg.llm_base_url, model=cfg.llm_model,
                    api_key=cfg.llm_api_key)
    if not llm.available():
        _LOGGER.warning("LLM endpoint unavailable (%s). Skipping this run.",
                        llm.base_url)
        return {"skipped_reason": "llm_unavailable"}

    cat = _catalog(cfg, gmail)
    motor = Engine(gmail, llm, cat, cfg)
    if not cfg.dry_run:
        motor.prune_logs()
    try:
        if mode == "full":
            motor.full()
        else:
            motor.incremental()
    except LLMUnavailable as e:
        _LOGGER.warning("LLM went down mid-run (%s). The next incremental "
                        "run catches up.", e)
        motor.stats["interrupted"] = "llm_unavailable"
    motor.stats["last_run"] = _now_iso()
    # per-run report (any mode): last-run.json + servable HTML, link in stats
    try:
        link = _write_reports(cfg, motor.records, motor.stats, mode)
        if link:
            motor.stats["report_link"] = link
    except Exception:  # noqa: BLE001 — report is best-effort, never fail the run
        _LOGGER.exception("Failed to write the run report")
    return motor.stats


def run_suggestor(access_token: str, cfg: EngineConfig, max_n: int) -> dict:
    """Sample the mailbox, suggest NEW categories, and preview where each
    sampled email would land (existing + suggested). Writes an interactive
    HTML report (checkboxes + one-click accept via webhook). Returns
    {suggestions, report_link}."""
    from . import sugestor

    gmail = GmailClient(access_token)
    llm = LLMClient(base_url=cfg.llm_base_url, model=cfg.llm_model,
                    api_key=cfg.llm_api_key)
    if not llm.available():
        raise LLMUnavailable(f"endpoint unavailable: {llm.base_url}")
    cat = _catalog(cfg, gmail)   # don't re-suggest existing labels either
    metas = sugestor.sample(gmail, max_n)
    sugestoes = sugestor.suggest(metas, cat, llm, log=_LOGGER)
    sugestor.save_json(cfg.account_dir, sugestoes)

    # 2nd pass: map each sampled email into existing + suggested categories
    names = list(cat.names) + [s["nome"] for s in sugestoes]
    descr = dict(cat.descriptions)
    for s in sugestoes:
        descr[s["nome"]] = s.get("descricao", "")
    distribuicao = sugestor.distribute(metas, names, descr, cat.review,
                                       llm, log=_LOGGER)
    sugeridas = {s["nome"] for s in sugestoes}
    link = None
    try:
        link = _write_suggestions_html(cfg, sugestoes, distribuicao, sugeridas)
    except Exception:  # noqa: BLE001 — report is best-effort
        _LOGGER.exception("Failed to write the suggestions report")
    return {"suggestions": sugestoes, "report_link": link}


def accept_suggestions(account_dir: str, numbers: str,
                      access_token: str | None = None) -> list[str]:
    """Apply saved suggestions ('1,3' or 'all'). Returns the added names.

    Adds them to categorias.yaml and, when a token is given, autonomously
    CREATES the corresponding Gmail labels right away (per account) — so they
    show up in Gmail immediately, not only on the next run.
    """
    from . import sugestor

    sugestoes = sugestor.load_json(account_dir)
    if numbers.strip().lower() in ("all", "todas", "todos"):
        accepted = sugestoes
    else:
        idx = [int(t) for t in numbers.split(",") if t.strip().isdigit()]
        accepted = [sugestoes[i - 1] for i in idx if 1 <= i <= len(sugestoes)]
    if not accepted:
        return []
    sugestor.apply_accepts(
        os.path.join(account_dir, "categorias.yaml"), accepted)
    names = [a["nome"] for a in accepted]
    if access_token:
        gmail = GmailClient(access_token)
        for n in names:
            try:
                gmail.ensure_label(n)
            except Exception:  # noqa: BLE001 — created lazily on next run anyway
                _LOGGER.exception("Could not create Gmail label %r", n)
    return names


# ------------------------------------------------------------------ report
# Human-readable destination + presentation order/style, per action.
_ACOES = {
    "trash":   ("🗑️", "Enviados para a Lixeira", "trash"),
    "shadow":  ("🌓", "Candidatos à Lixeira (modo sombra)", "shadow"),
    "archive": ("📥", "Arquivados (fora da Caixa de Entrada)", "archive"),
    "review":  ("👀", "Para revisar", "review"),
    "label":   ("🏷️", "Apenas rotulados (seguem na Caixa de Entrada)", "label"),
}
_ORDEM = ["trash", "shadow", "archive", "review", "label"]


def _target(plano: "Plan", cat: Catalog) -> str:
    """Frase curta de 'onde foi parar', para o relatório."""
    if plano.action == "trash":
        return "Lixeira"
    if plano.action == "shadow":
        return f"Caixa de Entrada + label “{cat.label_trash_candidate}”"
    if plano.action == "archive":
        cat_label = next((n for n in plano.add_labels
                          if n not in (cat.label_processed,)), "")
        return f"Arquivado sob “{cat_label}”" if cat_label else "Arquivado"
    if plano.action == "review":
        return f"Caixa de Entrada + label “{cat.review}”"
    cat_label = next((n for n in plano.add_labels
                      if n != cat.label_processed), "")
    return f"Caixa de Entrada + label “{cat_label}”" if cat_label \
        else "Caixa de Entrada"


def _write_reports(cfg: EngineConfig, records: list[dict],
                         stats: dict, mode: str) -> str | None:
    """Grava last-run.json (privado, no account_dir) e um HTML servível.

    Retorna o path /local/... do HTML (para o link da notificação) ou None.
    """
    doc = {
        "gerado_em": _now_iso(),
        "conta": os.path.basename(cfg.account_dir),
        "modo": mode,
        "simulacao": cfg.dry_run,
        "resumo": {k: v for k, v in stats.items()
                   if k in ("processed", "label", "archive", "review",
                            "trash", "shadow", "skipped")},
        "itens": records,
    }
    # 1) JSON estruturado, privado (sempre)
    with open(os.path.join(cfg.account_dir, "last-run.json"),
              "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    # 2) HTML servível via /local (nome contém token não-adivinhável)
    if not cfg.report_dir or not cfg.report_token:
        return None
    os.makedirs(cfg.report_dir, exist_ok=True)
    nome = f"report-{cfg.report_token}.html"
    with open(os.path.join(cfg.report_dir, nome), "w", encoding="utf-8") as f:
        f.write(_report_html(doc))
    return f"/local/polaris/{nome}"


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# CSS/JS kept as plain constants (single braces) — interpolated as values into
# the report f-string, so their braces never collide with f-string syntax.
_REPORT_CSS = """
:root{color-scheme:light dark}
*{box-sizing:border-box}
body{font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
margin:0;background:#f5f6f8;color:#1c2430}
@media(prefers-color-scheme:dark){body{background:#12151a;color:#e7eaef}
table,section,.controls{background:#1b2028!important;border-color:#2a313c!important}
th{color:#98a1b0!important}
input,select{background:#12151a!important;color:#e7eaef!important;border-color:#2a313c!important}}
header{padding:20px;background:#2b6cb0;color:#fff}
header h1{margin:0;font-size:20px} header .meta{opacity:.9;font-size:13px;margin-top:4px}
main{max-width:1000px;margin:0 auto;padding:16px 20px 60px}
.sim{background:#fef3c7;color:#92400e;padding:10px 14px;border-radius:8px;
margin:14px 0;font-weight:600}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}
.chip{padding:5px 11px;border-radius:999px;font-size:13px;font-weight:600;
background:#e5e9f0;color:#1c2430;border:0;cursor:pointer;user-select:none}
.chip.off{opacity:.3}
.chip.trash{background:#fde2e1;color:#b42318} .chip.shadow{background:#e9e3fb;color:#5b21b6}
.chip.archive{background:#dbeafe;color:#1e40af} .chip.review{background:#fef3c7;color:#92400e}
.chip.label{background:#dcfce7;color:#166534}
.controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap;
background:#fff;border:1px solid #e3e6ec;border-radius:10px;padding:10px 12px;margin:12px 0}
.controls input,.controls select{padding:7px 10px;border:1px solid #d7dbe3;
border-radius:8px;font-size:14px}
.btn{padding:7px 12px;border:0;border-radius:8px;background:#2b6cb0;color:#fff;
font-size:13px;font-weight:600;cursor:pointer}
#q{flex:1;min-width:180px} #conf{width:70px}
.controls .cf{font-size:13px;color:#6b7484;display:flex;align-items:center;gap:6px}
.controls .hint{font-size:12px;color:#98a1b0;flex-basis:100%}
section{background:#fff;border:1px solid #e3e6ec;border-radius:10px;
margin:16px 0;overflow:hidden}
h2{font-size:15px;margin:0;padding:12px 16px;border-bottom:1px solid #e3e6ec}
h2 .n{background:#00000018;padding:1px 8px;border-radius:999px;font-size:12px;margin-left:6px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 16px;border-bottom:1px solid #e3e6ec;vertical-align:top}
th{font-size:11px;text-transform:uppercase;color:#6b7484;font-weight:600;cursor:pointer;white-space:nowrap}
th:hover{color:#2b6cb0}
td.sub{font-weight:600;max-width:320px} td.num{text-align:right;font-variant-numeric:tabular-nums}
td.reason{color:#6b7484;max-width:260px}
tr:last-child td{border-bottom:0}
.empty{padding:20px;color:#98a1b0;text-align:center}
"""

_REPORT_JS = """
(function(){
  var q=document.getElementById('q'), cat=document.getElementById('cat'),
      conf=document.getElementById('conf'),
      chips=[].slice.call(document.querySelectorAll('.chip')),
      secs=[].slice.call(document.querySelectorAll('section[data-action]')),
      off={};
  function apply(){
    var term=(q.value||'').toLowerCase(), c=cat.value, mc=parseFloat(conf.value)||0,
        vis={};
    secs.forEach(function(sec){
      var act=sec.getAttribute('data-action'), n=0;
      if(off[act]){ sec.style.display='none'; vis[act]=0; return; }
      [].slice.call(sec.querySelectorAll('tbody tr')).forEach(function(tr){
        var okC=(c==='__all__')||tr.getAttribute('data-cat')===c,
            okF=parseFloat(tr.getAttribute('data-conf'))>=mc,
            okT=!term||tr.textContent.toLowerCase().indexOf(term)>=0,
            show=okC&&okF&&okT;
        tr.style.display=show?'':'none'; if(show)n++;
      });
      vis[act]=n; sec.style.display=n?'':'none';
      var nn=sec.querySelector('.n'); if(nn)nn.textContent=n;
    });
    chips.forEach(function(ch){
      var a=ch.getAttribute('data-action'), cn=ch.querySelector('.cn');
      if(cn&&vis[a]!==undefined)cn.textContent=vis[a];
    });
  }
  q.addEventListener('input',apply); cat.addEventListener('change',apply);
  conf.addEventListener('input',apply);
  chips.forEach(function(ch){ ch.addEventListener('click',function(){
    var a=ch.getAttribute('data-action');
    off[a]=!off[a]; ch.classList.toggle('off',!!off[a]); apply();
  });});
  var DEST={trash:'Lixeira',shadow:'Candidato a Lixeira',archive:'Arquivado',
            review:'Revisar',label:'Rotulado'};
  function cell(s){s=(s==null?'':''+s);
    return /[",\\n;]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;}
  document.getElementById('csv').addEventListener('click',function(){
    var rows=[['Destino','Assunto','Remetente','Categoria','Confianca','Motivo']];
    secs.forEach(function(sec){
      if(sec.style.display==='none')return;
      var d=DEST[sec.getAttribute('data-action')]||sec.getAttribute('data-action');
      [].slice.call(sec.querySelectorAll('tbody tr')).forEach(function(tr){
        if(tr.style.display==='none')return;
        var c=tr.children;
        rows.push([d,c[0].textContent,c[1].textContent,c[2].textContent,
                   c[3].textContent,c[4].textContent]);
      });
    });
    var csv='\\ufeff'+rows.map(function(r){return r.map(cell).join(',');}).join('\\n');
    var a=document.createElement('a');
    a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));
    a.download='polaris-relatorio.csv';document.body.appendChild(a);a.click();
    setTimeout(function(){URL.revokeObjectURL(a.href);a.remove();},100);
  });
  document.querySelectorAll('th[data-sort]').forEach(function(th){
    th.addEventListener('click',function(){
      var table=th.closest('table'), tb=table.querySelector('tbody'),
          idx=[].indexOf.call(th.parentNode.children,th),
          num=th.getAttribute('data-sort')==='num',
          asc=!(th.__asc); th.__asc=asc,
          rows=[].slice.call(tb.querySelectorAll('tr'));
      rows.sort(function(a,b){
        var x=a.children[idx].textContent.trim(), y=b.children[idx].textContent.trim();
        if(num){x=parseFloat(x)||0;y=parseFloat(y)||0;return asc?x-y:y-x;}
        return asc?x.localeCompare(y):y.localeCompare(x);
      });
      rows.forEach(function(r){tb.appendChild(r);});
    });
  });
})();
"""


def _report_html(doc: dict) -> str:
    itens = doc["itens"]
    por_acao: dict[str, list] = {}
    for r in itens:
        por_acao.setdefault(r["action"], []).append(r)
    conta = _esc(doc["conta"])
    sim = doc["simulacao"]
    quando = _esc(doc["gerado_em"][:19].replace("T", " "))
    modo = "Completo" if doc["modo"] == "full" else "Incremental"

    cats = sorted({r["category"] for r in itens})
    cat_opts = "".join(f'<option value="{_esc(c)}">{_esc(c)}</option>'
                       for c in cats)

    chips = "".join(
        f'<button type="button" class="chip {css} on" data-action="{k}">'
        f'{ic} <span class="cn">{_esc(len(por_acao.get(k, [])))}</span> '
        f'{_esc(titulo.split(" (")[0])}</button>'
        for k, (ic, titulo, css) in _ACOES.items() if por_acao.get(k)
    )

    sections = []
    for k in _ORDEM:
        lines = por_acao.get(k)
        if not lines:
            continue
        ic, titulo, css = _ACOES[k]
        lines = sorted(lines, key=lambda r: -r["confidence"])
        trs = "".join(
            f"<tr data-cat=\"{_esc(r['category'])}\" data-conf=\"{r['confidence']:.2f}\">"
            f"<td class='sub'>{_esc(r['subject']) or '—'}</td>"
            f"<td>{_esc(r['sender'])}</td>"
            f"<td>{_esc(r['category'])}</td>"
            f"<td class='num'>{r['confidence']:.2f}</td>"
            f"<td class='motivo'>{_esc(r['reason'])}</td></tr>"
            for r in lines
        )
        sections.append(
            f"<section class='{css}' data-action='{k}'><h2>{ic} {_esc(titulo)} "
            f"<span class='n'>{len(lines)}</span></h2>"
            "<table><thead><tr>"
            "<th data-sort='text'>Assunto</th><th data-sort='text'>Remetente</th>"
            "<th data-sort='text'>Categoria</th><th data-sort='num'>Conf.</th>"
            "<th data-sort='text'>Motivo</th></tr></thead>"
            f"<tbody>{trs}</tbody></table></section>"
        )

    banner = ('<div class="sim">MODO SIMULAÇÃO — nada foi alterado no Gmail</div>'
              if sim else "")
    controles = (
        '<div class="controls">'
        '<input id="q" type="search" placeholder="🔎 Buscar remetente, assunto, motivo…">'
        f'<select id="cat"><option value="__all__">Todas as categorias</option>{cat_opts}</select>'
        '<label class="cf">Conf. mín. '
        '<input id="conf" type="number" min="0" max="1" step="0.05" value="0"></label>'
        '<button type="button" id="csv" class="btn">⬇ Baixar CSV</button>'
        '<span class="hint">clique nas caixas p/ mostrar/ocultar · clique no cabeçalho '
        'p/ ordenar · o CSV baixa o que estiver visível (após os filtros)</span>'
        '</div>'
    )
    corpo = "".join(sections) or '<p>Nada processado nesta execução.</p>'
    return f"""<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polaris — relatório {conta}</title><style>{_REPORT_CSS}</style></head><body>
<header><h1>🧭 Polaris — {conta}</h1>
<div class="meta">Execução {modo} · {quando} UTC · {_esc(len(itens))} e-mails</div></header>
<main>{banner}<div class="chips">{chips}</div>{controles}{corpo}</main>
<script>{_REPORT_JS}</script>
</body></html>"""


# ---------------------------------------------------------- suggestions report
_SUGGEST_CSS = """
.intro{color:#6b7484;margin:6px 0 14px}
.bar{position:sticky;top:0;background:#f5f6f8;padding:12px 0;z-index:5;
display:flex;gap:12px;align-items:center;flex-wrap:wrap}
@media(prefers-color-scheme:dark){.bar{background:#12151a}}
section.sug h2{background:#f5f0ff}
@media(prefers-color-scheme:dark){section.sug h2{background:#241b3a!important}}
section.sug{border-color:#c4b5fd}
h2 label{display:inline-flex;gap:8px;align-items:center;cursor:pointer;font-weight:700}
h2 label input{width:17px;height:17px}
.desc{color:#6b7484;font-size:13px;padding:0 16px 4px}
details{background:#fff;border:1px solid #e3e6ec;border-radius:10px;margin:8px 0;padding:2px 14px}
@media(prefers-color-scheme:dark){details{background:#1b2028;border-color:#2a313c}}
summary{cursor:pointer;font-weight:600;padding:8px 0;font-size:14px}
#accmsg{font-weight:600;color:#2f855a}
"""

_SUGGEST_JS = """
(function(){
  var btn=document.getElementById('accept'), msg=document.getElementById('accmsg');
  if(!btn)return;
  btn.addEventListener('click',function(){
    var nums=[].slice.call(document.querySelectorAll('.acc:checked')).map(function(c){return c.value;});
    if(!nums.length){msg.textContent='Selecione ao menos uma categoria.';return;}
    btn.disabled=true; btn.textContent='Criando…'; msg.textContent='';
    fetch(WEBHOOK,{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({numbers:nums.join(',')})})
    .then(function(r){return r.json().catch(function(){return{};});})
    .then(function(j){
      var add=(j&&j.added&&j.added.length)?j.added.join(', '):nums.join(', ');
      msg.textContent='✅ Criado no Gmail: '+add; btn.textContent='Aceito ✓';
    }).catch(function(e){
      btn.disabled=false; btn.textContent='Aceitar selecionadas';
      msg.textContent='Erro ao aceitar: '+e;
    });
  });
})();
"""


def _write_suggestions_html(cfg: EngineConfig, sugestoes: list[dict],
                            distribuicao: list[dict], sugeridas: set) -> str | None:
    if not cfg.report_dir or not cfg.report_token:
        return None
    os.makedirs(cfg.report_dir, exist_ok=True)
    nome = f"suggest-{cfg.report_token}.html"
    doc = {
        "conta": os.path.basename(cfg.account_dir),
        "gerado_em": _now_iso(),
        "sugestoes": sugestoes,
        "distribuicao": distribuicao,
        "sugeridas": sugeridas,
        "webhook_url": cfg.webhook_url,
    }
    with open(os.path.join(cfg.report_dir, nome), "w", encoding="utf-8") as f:
        f.write(_suggestions_html(doc))
    return f"/local/polaris/{nome}"


def _email_table(itens: list[dict]) -> str:
    trs = "".join(
        f"<tr><td class='sub'>{_esc(m.get('assunto')) or '—'}</td>"
        f"<td>{_esc(m.get('remetente'))}</td></tr>"
        for m in itens)
    return ("<table><thead><tr><th>Assunto</th><th>Remetente</th></tr></thead>"
            f"<tbody>{trs}</tbody></table>")


def _suggestions_html(doc: dict) -> str:
    conta = _esc(doc["conta"])
    quando = _esc(doc["gerado_em"][:19].replace("T", " "))
    sugestoes = doc["sugestoes"]
    sugeridas = doc["sugeridas"]
    porcat: dict[str, list] = {}
    for m in doc["distribuicao"]:
        porcat.setdefault(m.get("categoria", "Revisar"), []).append(m)

    secoes_sug = []
    for i, s in enumerate(sugestoes, 1):
        nome = s["nome"]
        itens = porcat.get(nome, [])
        secoes_sug.append(
            f"<section class='sug'><h2><label>"
            f"<input class='acc' type='checkbox' value='{i}' checked> ✨ {_esc(nome)}"
            f"</label> <span class='n'>{len(itens)}</span></h2>"
            f"<p class='desc'>{_esc(s.get('descricao',''))}</p>"
            + (_email_table(itens) if itens else
               "<p class='desc'>Nenhum e-mail da amostra caiu aqui — o modelo "
               "propôs pelo tema geral.</p>")
            + "</section>")

    existing = []
    for nome in sorted(porcat):
        if nome in sugeridas:
            continue
        itens = porcat[nome]
        existing.append(
            f"<details><summary>{_esc(nome)} <span class='n'>{len(itens)}</span>"
            f"</summary>{_email_table(itens)}</details>")

    tem_wh = bool(doc["webhook_url"])
    barra = (
        '<div class="bar"><button id="accept" class="btn">Aceitar selecionadas'
        '</button><span id="accmsg"></span></div>' if tem_wh else
        '<div class="msg">As sugestões estão pré-marcadas. Aceite chamando o '
        'serviço <code>polaris.accept_categories</code> com os números.</div>')
    wh_js = (f"var WEBHOOK={doc['webhook_url']!r};{_SUGGEST_JS}"
             if tem_wh else "")
    return f"""<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polaris — sugestões {conta}</title>
<style>{_REPORT_CSS}{_SUGGEST_CSS}</style></head><body>
<header><h1>💡 Polaris — sugestões de categorias</h1>
<div class="meta">{conta} · {quando} UTC · {len(sugestoes)} sugestão(ões)</div></header>
<main>
<p class="intro">Marque as categorias novas que quer criar e clique em aceitar —
as labels são criadas no seu Gmail na hora. Abaixo, o que cairia em cada uma
(e nas categorias que você já tem).</p>
{barra}
{''.join(secoes_sug) or '<p>Nenhuma categoria nova a sugerir — as atuais já cobrem a amostra.</p>'}
<h2 style="margin-top:24px">Distribuição nas categorias existing</h2>
{''.join(existing) or '<p class="desc">—</p>'}
</main>
<script>{wh_js}</script>
</body></html>"""


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
