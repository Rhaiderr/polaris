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
import shutil
import sys
from dataclasses import dataclass

from .classificador import (Catalogo, Classificacao, carregar_catalogo,
                            carregar_prompt, classificar, seed_prompt_yaml)
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
# credentials.json é COMPARTILHADO; token/categorias/state são por-conta.
CATEGORIAS_EXEMPLO = os.path.join(CONFIG_DIR, "categorias.yaml.example")
LOCK_PATH = os.path.join(LOGS_DIR, ".polaris.lock")
CONTA_PADRAO = "principal"   # usado no --login quando --conta é omitido

log = logging.getLogger("polaris")


# ---------------------------------------------------------------- contas
def conta_dir(conta: str) -> str:
    """Diretório de config de uma conta: config/<conta>/ (token/categorias/state)."""
    return os.path.join(CONFIG_DIR, conta)


def perfis_configurados() -> list[str]:
    """Contas já logadas = subpastas de config/ que têm token.json."""
    if not os.path.isdir(CONFIG_DIR):
        return []
    return sorted(
        d for d in os.listdir(CONFIG_DIR)
        if os.path.isdir(os.path.join(CONFIG_DIR, d))
        and os.path.exists(os.path.join(CONFIG_DIR, d, "token.json"))
    )


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
    def __init__(self, conta: str, dry_run: bool, reprocessar: bool, max_n: int | None):
        self.conta = conta
        self.dry_run = dry_run
        self.reprocessar = reprocessar
        self.max_n = max_n
        cdir = conta_dir(conta)
        self.categorias_path = os.path.join(cdir, "categorias.yaml")
        self.state_path = os.path.join(cdir, "state.json")
        self.decisoes_path = os.path.join(LOGS_DIR, conta, "decisoes.jsonl")
        self.cat = carregar_catalogo(self.categorias_path)
        self.prompts = carregar_prompt(os.path.join(cdir, "prompt.yaml"))
        self.gmail = GmailClient(token_path=os.path.join(cdir, "token.json"))
        self.llm = LLMClient()
        self._label_id_cache: dict[str, str] = {}
        self.stats = {"vistos": 0, "processados": 0, "pulados": 0,
                      "revisar": 0, "arquivar": 0, "excluir": 0, "sombra": 0, "label": 0}

    # ---- estado (leitura/gravação atômica) ----
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
                cls = classificar(email, self.cat, self.llm, self.prompts)

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
            os.makedirs(os.path.dirname(self.decisoes_path), exist_ok=True)
            with open(self.decisoes_path, "a", encoding="utf-8") as f:
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

    def _prune_logs(self, retencao_dias: int) -> None:
        """Remove entradas do decisoes.jsonl (desta conta) mais antigas que a retenção."""
        if not os.path.exists(self.decisoes_path):
            return
        limite = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=retencao_dias)
        manter = []
        with open(self.decisoes_path, encoding="utf-8") as f:
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
        tmp = self.decisoes_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(manter) + ("\n" if manter else ""))
        os.replace(tmp, self.decisoes_path)

    def resumo(self) -> None:
        s = self.stats
        log.info("Conta '%s' — resumo: vistos=%d processados=%d pulados=%d | "
                 "label=%d arquivar=%d revisar=%d excluir=%d sombra=%d%s",
                 self.conta, s["vistos"], s["processados"], s["pulados"], s["label"],
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


def _onboarding(conta: str) -> int:
    """Adiciona uma conta: faz o login OAuth e deixa tudo pronto num comando.

    Mantém o fluxo simples para não assustar quem nunca configurou nada:
    cria config/<conta>/, gera o token, semeia um categorias.yaml inicial e
    imprime o próximo passo.
    """
    cdir = conta_dir(conta)
    os.makedirs(cdir, exist_ok=True)
    token_path = os.path.join(cdir, "token.json")
    try:
        GmailClient.autenticar_interativo(token_path=token_path)
    except FileNotFoundError as e:
        log.error("%s", e)
        return 1
    cat_path = os.path.join(cdir, "categorias.yaml")
    semeou = False
    if not os.path.exists(cat_path) and os.path.exists(CATEGORIAS_EXEMPLO):
        shutil.copy(CATEGORIAS_EXEMPLO, cat_path)
        semeou = True
    prompt_path = os.path.join(cdir, "prompt.yaml")
    if not os.path.exists(prompt_path):
        seed_prompt_yaml(prompt_path)
    log.info("")
    log.info("✅ Conta '%s' adicionada! Token em %s", conta, token_path)
    if semeou:
        log.info("   Criei um categorias.yaml inicial: %s", cat_path)
        log.info("   → Edite com as SUAS categorias/labels do Gmail (é só texto).")
    log.info("   Veja a triagem SEM aplicar nada:")
    log.info("       python -m src.orquestrador --conta %s --modo completo --dry-run --max 30",
             conta)
    return 0


def _rodar_conta(conta: str, args, retencao: int) -> int:
    """Roda a triagem de UMA conta. Retorna 0 em sucesso, 1 se falhou ao iniciar."""
    log.info("──── Conta '%s' ────", conta)
    try:
        orq = Orquestrador(conta, args.dry_run, args.reprocessar, args.max_n)
    except Exception as e:  # falta de token/categorias etc.
        log.error("Conta '%s': falha ao inicializar: %s", conta, e)
        return 1
    if not args.dry_run:
        orq._prune_logs(retencao)
    try:
        if args.modo == "incremental":
            orq.incremental()
        else:
            orq.completo()
    except LLMIndisponivel as e:
        log.warning("Conta '%s': LLM caiu no meio (%s). Sigo sem erro; "
                    "o incremental recupera na próxima rodada.", conta, e)
    finally:
        orq.resumo()
    return 0


def _sugerir_categorias(conta: str, args) -> int:
    """Fluxo de sugestão de categorias (futuro passo 3 do wizard do add-on).

    --sugerir-categorias: amostra a caixa → LLM propõe → terminal interativo
    aceita na hora; sem TTY, salva logs/<conta>/sugestoes.json e sai (o front
    lê esse JSON e devolve o aceite via --aceitar).
    --aceitar '1,3'|'todas': aplica sugestões salvas ao categorias.yaml.
    """
    from . import sugestor

    cdir = conta_dir(conta)
    categorias_path = os.path.join(cdir, "categorias.yaml")
    if not os.path.exists(os.path.join(cdir, "token.json")):
        log.error("Conta '%s' sem login. Rode: python -m src.orquestrador "
                  "--conta %s --login", conta, conta)
        return 1

    # ---- só aceite (front / segunda etapa) ----
    if args.aceitar and not args.sugerir:
        try:
            sugestoes = sugestor.carregar_json(conta, LOGS_DIR)
        except FileNotFoundError as e:
            log.error("%s", e)
            return 1
        if args.aceitar.strip().lower() in ("todas", "todos", "all"):
            aceitas = sugestoes
        else:
            idx = [int(t) for t in args.aceitar.split(",") if t.strip().isdigit()]
            aceitas = [sugestoes[i - 1] for i in idx if 1 <= i <= len(sugestoes)]
        if not aceitas:
            log.info("Nada a aceitar.")
            return 0
        sugestor.aplicar_aceites(categorias_path, aceitas)
        log.info("✅ %d categoria(s) adicionada(s) a %s: %s",
                 len(aceitas), categorias_path,
                 ", ".join(a["nome"] for a in aceitas))
        return 0

    # ---- varredura + sugestão ----
    try:
        cat = carregar_catalogo(categorias_path)
        gmail = GmailClient(token_path=os.path.join(cdir, "token.json"))
        llm = LLMClient()
    except Exception as e:
        log.error("Conta '%s': falha ao inicializar: %s", conta, e)
        return 1
    if not llm.disponivel():
        log.error("Endpoint LLM indisponível (%s). Suba o modelo e rode de novo.",
                  llm.base_url)
        return 1

    max_n = args.max_n or 200
    log.info("Analisando %d emails da conta '%s' (só remetente/assunto)...",
             max_n, conta)
    metas = sugestor.amostrar(gmail, max_n)
    try:
        sugestoes = sugestor.sugerir(metas, cat, llm, log=log)
    except LLMIndisponivel as e:
        log.error("LLM caiu durante a análise (%s). Rode de novo.", e)
        return 1
    if not sugestoes:
        log.info("Nenhuma categoria nova a sugerir — as atuais já cobrem a caixa.")
        return 0

    path = sugestor.salvar_json(conta, LOGS_DIR, sugestoes)
    if sugestor.interativo():
        aceitas = sugestor._prompt_checkbox(sugestoes)
        if not aceitas:
            log.info("Nenhuma aceita. Sugestões ficaram salvas em %s "
                     "(aceite depois com --aceitar).", path)
            return 0
        sugestor.aplicar_aceites(categorias_path, aceitas)
        log.info("✅ %d categoria(s) adicionada(s): %s",
                 len(aceitas), ", ".join(a["nome"] for a in aceitas))
    else:
        # Sem TTY (front/automação): só publica o JSON.
        print(json.dumps({"sugestoes": sugestoes}, ensure_ascii=False, indent=2))
        log.info("Sugestões salvas em %s. Aceite com: --conta %s --aceitar '1,2'",
                 path, conta)
    return 0


def main(argv=None) -> int:
    global MODO_SOMBRA
    ap = argparse.ArgumentParser(prog="polaris", description="Triagem de Gmail com LLM.")
    ap.add_argument("--conta", default=None,
                    help="qual conta processar (config/<conta>/). Omitido: TODAS as "
                         "contas configuradas (no --login: 'principal').")
    ap.add_argument("--modo", choices=["incremental", "completo"], default="incremental")
    ap.add_argument("--dry-run", action="store_true", help="não aplica nada no Gmail")
    ap.add_argument("--reprocessar", action="store_true",
                    help="não pula mensagens já marcadas com Polaris/Processado")
    ap.add_argument("--max", type=int, default=None, dest="max_n",
                    help="limita quantas mensagens processar")
    ap.add_argument("--login", action="store_true",
                    help="adiciona/reautentica uma conta (login OAuth) e sai")
    ap.add_argument("--sugerir-categorias", action="store_true", dest="sugerir",
                    help="varre uma amostra da conta e sugere categorias novas "
                         "(interativo no terminal; sem TTY salva JSON) e sai")
    ap.add_argument("--aceitar", default=None, metavar="NUMS",
                    help="aceita sugestões salvas (logs/<conta>/sugestoes.json): "
                         "'1,3' ou 'todas'. Usado pelo front / pós --sugerir-categorias")
    args = ap.parse_args(argv)

    _carregar_dotenv()
    _setup_logging()

    if args.login:
        return _onboarding(args.conta or CONTA_PADRAO)

    if args.sugerir or args.aceitar:
        if not args.conta:
            log.error("--sugerir-categorias/--aceitar exigem --conta <nome>.")
            return 1
        return _sugerir_categorias(args.conta, args)

    # Sem --conta: processa TODAS as contas configuradas (ideal para o timer).
    contas = [args.conta] if args.conta else perfis_configurados()
    if not contas:
        log.error("Nenhuma conta configurada. Adicione uma com: "
                  "python -m src.orquestrador --conta <nome> --login")
        return 1
    # Conta pedida explicitamente mas sem login ainda → mensagem clara.
    faltando = [c for c in contas
                if not os.path.exists(os.path.join(conta_dir(c), "token.json"))]
    if faltando:
        log.error("Conta(s) sem login: %s. Adicione com: "
                  "python -m src.orquestrador --conta %s --login",
                  ", ".join(faltando), faltando[0])
        return 1

    MODO_SOMBRA = _env_bool("MODO_SOMBRA_EXCLUSAO", True)
    retencao = int(os.environ.get("LOG_RETENCAO_DIAS", "90"))

    # Lock global contra execução concorrente (timer x execução manual).
    os.makedirs(LOGS_DIR, exist_ok=True)
    lock_fd = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.warning("Outra execução do Polaris está em andamento (lock). Saindo.")
        return 0

    try:
        # Endpoint LLM é compartilhado entre as contas — checa uma vez só.
        try:
            llm = LLMClient()
        except ValueError as e:
            log.error("Config do LLM inválida: %s", e)
            return 1
        if not llm.disponivel():
            log.warning("Endpoint LLM indisponível (%s). Pulando esta execução.",
                        llm.base_url)
            return 0

        rc = 0
        for conta in contas:
            rc |= _rodar_conta(conta, args, retencao)
        return rc
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == "__main__":
    raise SystemExit(main())
