"""Orquestrador — o cérebro do Polaris (CLI, decisão, aplicação, estado).

Fluxo: busca (incremental|completo) → pré-filtro → classifica → decide ação
segundo os limiares → aplica no Gmail (label / arquiva / trash|sombra) → loga.

Garantias de segurança embutidas:
- limiares conservadores (Revisar<0.70, arquivar≥0.80, excluir≥0.95);
- exclusão só para categoria elegível, COM List-Unsubscribe e em thread única;
- MODO_SOMBRA_EXCLUSAO: em vez de trash, aplica 'Polaris/Lixeira-candidata';
- idempotência via label 'Polaris/Processado';
- --dry-run não toca no Gmail;
- flock contra execução concorrente; state.json gravado atomicamente;
- endpoint LLM indisponível → pula a execução (exit 0), sem quebrar.

CLI:
  python -m src.orquestrador --login                 (1º OAuth, fora do container)
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
import sys
from dataclasses import dataclass

from .classificador import Catalogo, Classificacao, carregar_catalogo, classificar
from .gmail_client import EmailMsg, GmailClient, HistoryExpirada
from .llm_client import LLMClient, LLMIndisponivel
from . import prefiltro

# --- Limiares aprovados (Fase 2 §2.4) ---------------------------------------
LIMIAR_REVISAR = 0.70   # < isto → Revisar
LIMIAR_ARQUIVAR = 0.80  # ≥ isto e arquivar:true → remove da INBOX
LIMIAR_EXCLUIR = 0.95   # ≥ isto (+ demais critérios) → trash/sombra

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
CONFIG_DIR = os.path.join(BASE_DIR, "config")
LOGS_DIR = os.path.join(BASE_DIR, "logs")
STATE_PATH = os.path.join(CONFIG_DIR, "state.json")
CATEGORIAS_PATH = os.path.join(CONFIG_DIR, "categorias.yaml")
DECISOES_PATH = os.path.join(LOGS_DIR, "decisoes.jsonl")
LOCK_PATH = os.path.join(LOGS_DIR, ".polaris.lock")

log = logging.getLogger("polaris")


ENV_PATH = os.path.join(BASE_DIR, ".env")


def _carregar_dotenv(path: str = ENV_PATH) -> None:
    """Carrega .env para os.environ (execução local, fora do Docker).

    Parser mínimo (sem dependência externa): ignora linhas em branco e
    comentários, aceita `KEY=VALUE` (com `export ` opcional e aspas). Variáveis
    já presentes no ambiente têm precedência (não são sobrescritas) — assim o
    env_file do compose e o EnvironmentFile do systemd continuam mandando.
    """
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for linha in f:
            linha = linha.strip()
            if not linha or linha.startswith("#"):
                continue
            if linha.startswith("export "):
                linha = linha[len("export "):]
            if "=" not in linha:
                continue
            chave, _, valor = linha.partition("=")
            chave = chave.strip()
            valor = valor.strip().strip('"').strip("'")
            if chave and chave not in os.environ:
                os.environ[chave] = valor


def _env_bool(nome: str, default: bool) -> bool:
    v = os.environ.get(nome)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "sim", "on")


# ------------------------------------------------------------------ decisão
@dataclass
class Plano:
    add_labels: list[str]        # nomes de labels a adicionar (inclui Processado)
    remove_inbox: bool           # arquivar
    exclusao: str | None         # None | "trash" | "sombra"
    acao: str                    # rótulo humano p/ log: revisar|label|arquivar|excluir|sombra


def decidir(
    email: EmailMsg,
    cls: Classificacao,
    cat: Catalogo,
    modo_sombra: bool,
    contar_thread,   # callable() -> int (lazy: só chamado se for arquivar/excluir)
) -> Plano:
    """Traduz a classificação em ações concretas conforme os limiares."""
    add = [cat.label_processado]

    # JSON inválido OU baixa confiança → Revisar (não arquiva, não exclui).
    if cls.invalido or cls.confianca < LIMIAR_REVISAR:
        add.append(cat.revisar)
        return Plano(add, remove_inbox=False, exclusao=None, acao="revisar")

    # Confiança suficiente → aplica a label da categoria.
    if cls.categoria != cat.label_processado:
        add.append(cls.categoria)

    # Candidato a EXCLUSÃO? (tem prioridade sobre só arquivar)
    quer_excluir = (
        cls.excluir
        and cat.elegivel_exclusao(cls.categoria)
        and cls.confianca >= LIMIAR_EXCLUIR
        and email.tem_list_unsubscribe          # sinal determinístico obrigatório
    )
    if quer_excluir and contar_thread() == 1:   # só thread de mensagem única
        if modo_sombra:
            add.append(cat.label_lixeira_candidata)
            return Plano(add, remove_inbox=False, exclusao="sombra", acao="sombra")
        return Plano(add, remove_inbox=True, exclusao="trash", acao="excluir")

    # Candidato a ARQUIVAR? (categorias sensíveis podem vetar o auto-arquivamento)
    if (cls.arquivar and cls.confianca >= LIMIAR_ARQUIVAR
            and cat.elegivel_arquivamento(cls.categoria)
            and contar_thread() == 1):
        return Plano(add, remove_inbox=True, exclusao=None, acao="arquivar")

    # Caso contrário: só a label da categoria.
    return Plano(add, remove_inbox=False, exclusao=None, acao="label")


# ------------------------------------------------------------ orquestrador
class Orquestrador:
    def __init__(self, dry_run: bool, reprocessar: bool, max_n: int | None):
        self.dry_run = dry_run
        self.reprocessar = reprocessar
        self.max_n = max_n
        self.cat = carregar_catalogo(CATEGORIAS_PATH)
        self.gmail = GmailClient()
        self.llm = LLMClient()
        self._label_id_cache: dict[str, str] = {}
        self.stats = {"vistos": 0, "processados": 0, "pulados": 0,
                      "revisar": 0, "arquivar": 0, "excluir": 0, "sombra": 0, "label": 0}

    # ---- estado (leitura/gravação atômica) ----
    def _load_state(self) -> dict:
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        return {"historyId": None, "ultima_execucao": None}

    def _save_state(self, state: dict) -> None:
        if self.dry_run:
            return
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)

    # ---- label name -> id (cria se preciso) ----
    def _label_id(self, nome: str) -> str:
        if nome not in self._label_id_cache:
            self._label_id_cache[nome] = self.gmail.garantir_label(nome)
        return self._label_id_cache[nome]

    # ---- loop principal de mensagens ----
    def _processar(self, pares: list[dict]) -> None:
        processado_id = None if self.dry_run else self._label_id(self.cat.label_processado)
        for par in pares:
            if self.max_n and self.stats["processados"] >= self.max_n:
                log.info("--max %s atingido; parando.", self.max_n)
                break
            self.stats["vistos"] += 1
            email = self.gmail.get_email(par["id"])

            # idempotência: pula quem já tem Polaris/Processado (salvo --reprocessar)
            if not self.reprocessar and processado_id and processado_id in email.label_ids:
                self.stats["pulados"] += 1
                continue

            pf = prefiltro.aplicar(email)
            if pf.pular_llm and pf.categoria:
                cls = Classificacao(pf.categoria, False, False, pf.confianca, pf.motivo)
            else:
                cls = classificar(email, self.cat, self.llm)

            contador = _memo(lambda: self.gmail.contar_mensagens_thread(email.thread_id))
            plano = decidir(email, cls, self.cat, MODO_SOMBRA, contador)

            self._aplicar(email, plano)
            self._logar(email, cls, plano)
            self.stats["processados"] += 1
            self.stats[plano.acao] = self.stats.get(plano.acao, 0) + 1

    def _aplicar(self, email: EmailMsg, plano: Plano) -> None:
        if self.dry_run:
            return
        add_ids = [self._label_id(n) for n in plano.add_labels]
        remove_ids = ["INBOX"] if plano.remove_inbox else []
        if plano.exclusao == "trash":
            # aplica labels primeiro (rastro), depois manda p/ Lixeira
            self.gmail.modificar(email.id, add=add_ids, remove=remove_ids)
            self.gmail.trash(email.id)
        else:
            self.gmail.modificar(email.id, add=add_ids, remove=remove_ids)

    def _logar(self, email: EmailMsg, cls: Classificacao, plano: Plano) -> None:
        registro = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "id": email.id,
            "thread": email.thread_id,
            "remetente": email.remetente,
            "assunto": email.assunto,
            "categoria": cls.categoria,
            "confianca": cls.confianca,
            "arquivar": cls.arquivar,
            "excluir": cls.excluir,
            "motivo": cls.motivo,
            "acao": plano.acao,
            "dry_run": self.dry_run,
        }
        if self.dry_run:
            log.info("[DRY] %s → %s [cat=%s conf=%.2f excluir=%s unsub=%s] %s",
                     (email.assunto or "(sem assunto)")[:50], plano.acao,
                     cls.categoria, cls.confianca, cls.excluir,
                     email.tem_list_unsubscribe, cls.motivo)
        else:
            with open(DECISOES_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(registro, ensure_ascii=False) + "\n")

    # ---- modos ----
    def incremental(self) -> None:
        state = self._load_state()
        if not state.get("historyId"):
            # Bootstrap: fixa o cursor atual; backlog fica para o modo completo.
            prof = self.gmail.get_profile()
            state["historyId"] = prof["historyId"]
            state["ultima_execucao"] = _agora_iso()
            self._save_state(state)
            log.info("Bootstrap: cursor historyId fixado (%s). Nada a processar. "
                     "Rode --modo completo para o backlog.", prof["historyId"])
            return
        try:
            pares, novo_hid = self.gmail.history_added(state["historyId"])
            log.info("Incremental: %d mensagem(ns) nova(s) desde o último cursor.", len(pares))
        except HistoryExpirada:
            # Cursor velho demais: fallback por data + recontar o cursor.
            depois = _after_query(state.get("ultima_execucao"))
            log.warning("historyId expirado; fallback messages.list %s", depois)
            pares = self.gmail.messages_list(depois)
            novo_hid = self.gmail.get_profile()["historyId"]
        self._processar(pares)
        state["historyId"] = novo_hid or state["historyId"]
        state["ultima_execucao"] = _agora_iso()
        self._save_state(state)

    def completo(self) -> None:
        query = "-in:chats"
        if not self.reprocessar:
            query += f' -label:"{self.cat.label_processado}"'
        pares = self.gmail.messages_list(query, max_results=self.max_n)
        log.info("Completo: %d mensagem(ns) candidata(s) (query: %s).", len(pares), query)
        self._processar(pares)
        state = self._load_state()
        state["historyId"] = self.gmail.get_profile()["historyId"]
        state["ultima_execucao"] = _agora_iso()
        self._save_state(state)

    def resumo(self) -> None:
        s = self.stats
        log.info("Resumo: vistos=%d processados=%d pulados=%d | "
                 "label=%d arquivar=%d revisar=%d excluir=%d sombra=%d%s",
                 s["vistos"], s["processados"], s["pulados"], s["label"],
                 s["arquivar"], s["revisar"], s["excluir"], s["sombra"],
                 "  (DRY-RUN: nada aplicado)" if self.dry_run else "")


# ------------------------------------------------------------------ util
def _agora_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _after_query(ultima_execucao: str | None) -> str:
    """Query messages.list a partir da última execução (com 1 dia de folga)."""
    if ultima_execucao:
        try:
            base = dt.datetime.fromisoformat(ultima_execucao)
        except ValueError:
            base = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)
    else:
        base = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)
    base -= dt.timedelta(days=1)  # folga para não perder mensagens na fronteira
    return f"after:{base.strftime('%Y/%m/%d')} -in:chats"


def _memo(fn):
    """Memoiza um callable de zero args (avalia no máximo 1x)."""
    cache = {}
    def wrapped():
        if "v" not in cache:
            cache["v"] = fn()
        return cache["v"]
    return wrapped


def _prune_logs(retencao_dias: int) -> None:
    """Remove entradas de decisoes.jsonl mais antigas que a retenção (contêm emails)."""
    if not os.path.exists(DECISOES_PATH):
        return
    limite = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=retencao_dias)
    manter = []
    with open(DECISOES_PATH, encoding="utf-8") as f:
        for linha in f:
            linha = linha.strip()
            if not linha:
                continue
            try:
                ts = dt.datetime.fromisoformat(json.loads(linha)["ts"])
                if ts >= limite:
                    manter.append(linha)
            except (json.JSONDecodeError, KeyError, ValueError):
                manter.append(linha)  # não descarta o que não sei datar
    tmp = DECISOES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(manter) + ("\n" if manter else ""))
    os.replace(tmp, DECISOES_PATH)


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


# globais lidos do ambiente (definidos em main após carregar .env)
MODO_SOMBRA = True


def main(argv=None) -> int:
    global MODO_SOMBRA
    ap = argparse.ArgumentParser(prog="polaris", description="Triagem de Gmail com LLM.")
    ap.add_argument("--modo", choices=["incremental", "completo"], default="incremental")
    ap.add_argument("--dry-run", action="store_true", help="não aplica nada no Gmail")
    ap.add_argument("--reprocessar", action="store_true",
                    help="não pula mensagens já marcadas com Polaris/Processado")
    ap.add_argument("--max", type=int, default=None, dest="max_n",
                    help="limita quantas mensagens processar")
    ap.add_argument("--login", action="store_true",
                    help="faz o 1º login OAuth (gera config/token.json) e sai")
    args = ap.parse_args(argv)

    _carregar_dotenv()
    _setup_logging()

    if args.login:
        GmailClient.autenticar_interativo()
        log.info("token.json gerado em config/. Login concluído.")
        return 0

    MODO_SOMBRA = _env_bool("MODO_SOMBRA_EXCLUSAO", True)
    retencao = int(os.environ.get("LOG_RETENCAO_DIAS", "90"))

    # Lock contra execução concorrente (timer x execução manual).
    os.makedirs(LOGS_DIR, exist_ok=True)
    lock_fd = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.warning("Outra execução do Polaris está em andamento (lock). Saindo.")
        return 0

    try:
        orq = Orquestrador(args.dry_run, args.reprocessar, args.max_n)
    except Exception as e:  # falta de token/config etc.
        log.error("Falha ao inicializar: %s", e)
        return 1

    # Endpoint LLM indisponível → pular execução (exit 0). Incremental recupera depois.
    if not orq.llm.disponivel():
        log.warning("Endpoint LLM indisponível (%s). Pulando esta execução.",
                    orq.llm.base_url)
        return 0

    if not args.dry_run:
        _prune_logs(retencao)

    try:
        if args.modo == "incremental":
            orq.incremental()
        else:
            orq.completo()
    except LLMIndisponivel as e:
        log.warning("LLM caiu no meio da execução (%s). Interrompendo sem erro; "
                    "o incremental recupera na próxima rodada.", e)
    finally:
        orq.resumo()
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
