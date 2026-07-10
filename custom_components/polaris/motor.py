"""Motor de triagem — o Orquestrador do Polaris adaptado à integração HA.

Mesmo fluxo e MESMAS garantias de segurança da versão CLI:
busca (incremental|completo) → pré-filtro → classifica → decide ação
segundo os limiares → aplica no Gmail (label / arquiva / trash|sombra) → loga.

- limiares conservadores (Revisar<0.70, arquivar≥0.80, excluir≥0.95);
- exclusão só p/ categoria elegível, COM List-Unsubscribe e em thread única;
- modo sombra: em vez de trash, aplica 'Polaris/Lixeira-candidata';
- idempotência via label 'Polaris/Processado';
- dry_run não toca no Gmail;
- endpoint LLM indisponível → pula a execução (sem erro).

Diferenças para a CLI: o access token vem do Home Assistant (OAuth2Session,
renovado por ele); config/estado/auditoria vivem em /config/polaris/<email>/;
tudo aqui é SÍNCRONO — o runtime chama via executor.
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

# --- Limiares aprovados (Fase 2 §2.4) ---------------------------------------
LIMIAR_REVISAR = 0.70   # < isto → Revisar
LIMIAR_ARQUIVAR = 0.80  # ≥ isto e arquivar:true → remove da INBOX
LIMIAR_EXCLUIR = 0.95   # ≥ isto (+ demais critérios) → trash/sombra

RETENCAO_LOG_DIAS = 90

_LOGGER = logging.getLogger(__name__)

CATEGORIAS_EXEMPLO = os.path.join(os.path.dirname(__file__),
                                  "categorias.yaml.example")


@dataclass
class MotorConfig:
    """Tudo que uma execução precisa (vem das opções da config entry)."""
    conta_dir: str          # /config/polaris/<email>
    llm_base_url: str
    llm_model: str
    llm_api_key: str = ""
    modo_sombra: bool = True
    dry_run: bool = False
    reprocessar: bool = False
    max_n: int | None = None


def preparar_conta_dir(conta_dir: str) -> None:
    """Cria o diretório da conta e semeia um categorias.yaml inicial."""
    os.makedirs(conta_dir, exist_ok=True)
    cat_path = os.path.join(conta_dir, "categorias.yaml")
    if not os.path.exists(cat_path) and os.path.exists(CATEGORIAS_EXEMPLO):
        shutil.copy(CATEGORIAS_EXEMPLO, cat_path)
        _LOGGER.info("categorias.yaml inicial criado em %s — edite com as "
                     "suas labels do Gmail", cat_path)


def construir_service(access_token: str):
    """Monta o client do Gmail a partir do access token do HA."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials(token=access_token)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


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


# ------------------------------------------------------------------ motor
class Motor:
    def __init__(self, gmail: GmailClient, llm: LLMClient, cat: Catalogo,
                 cfg: MotorConfig):
        self.gmail = gmail
        self.llm = llm
        self.cat = cat
        self.cfg = cfg
        self.state_path = os.path.join(cfg.conta_dir, "state.json")
        self.decisoes_path = os.path.join(cfg.conta_dir, "decisoes.jsonl")
        self._label_id_cache: dict[str, str] = {}
        self.stats = {"vistos": 0, "processados": 0, "pulados": 0,
                      "revisar": 0, "arquivar": 0, "excluir": 0,
                      "sombra": 0, "label": 0}

    # ---- estado (leitura/gravação atômica) ----
    def _load_state(self) -> dict:
        if os.path.exists(self.state_path):
            with open(self.state_path, encoding="utf-8") as f:
                return json.load(f)
        return {"historyId": None, "ultima_execucao": None}

    def _save_state(self, state: dict) -> None:
        if self.cfg.dry_run:
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
        processado_id = (None if self.cfg.dry_run
                         else self._label_id(self.cat.label_processado))
        for par in pares:
            if self.cfg.max_n and self.stats["processados"] >= self.cfg.max_n:
                _LOGGER.info("Limite de %s mensagens atingido; parando.",
                             self.cfg.max_n)
                break
            self.stats["vistos"] += 1
            email = self.gmail.get_email(par["id"])

            # idempotência: pula quem já tem Polaris/Processado
            if (not self.cfg.reprocessar and processado_id
                    and processado_id in email.label_ids):
                self.stats["pulados"] += 1
                continue

            pf = prefiltro.aplicar(email)
            if pf.pular_llm and pf.categoria:
                cls = Classificacao(pf.categoria, False, False,
                                    pf.confianca, pf.motivo)
            else:
                cls = classificar(email, self.cat, self.llm)

            contador = _memo(
                lambda: self.gmail.contar_mensagens_thread(email.thread_id))
            plano = decidir(email, cls, self.cat, self.cfg.modo_sombra, contador)

            self._aplicar(email, plano)
            self._logar(email, cls, plano)
            self.stats["processados"] += 1
            self.stats[plano.acao] = self.stats.get(plano.acao, 0) + 1

    def _aplicar(self, email: EmailMsg, plano: Plano) -> None:
        if self.cfg.dry_run:
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
            "dry_run": self.cfg.dry_run,
        }
        if self.cfg.dry_run:
            _LOGGER.info("[DRY] %s → %s [cat=%s conf=%.2f excluir=%s unsub=%s] %s",
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
            _LOGGER.info("Bootstrap: cursor historyId fixado (%s). Nada a "
                         "processar. Rode o serviço com modo 'completo' para "
                         "o backlog.", prof["historyId"])
            self.stats["bootstrap"] = True
            return
        try:
            pares, novo_hid = self.gmail.history_added(state["historyId"])
            _LOGGER.info("Incremental: %d mensagem(ns) nova(s) desde o último "
                         "cursor.", len(pares))
        except HistoryExpirada:
            # Cursor velho demais: fallback por data + recontar o cursor.
            depois = _after_query(state.get("ultima_execucao"))
            _LOGGER.warning("historyId expirado; fallback messages.list %s",
                            depois)
            pares = self.gmail.messages_list(depois)
            novo_hid = self.gmail.get_profile()["historyId"]
        self._processar(pares)
        state["historyId"] = novo_hid or state["historyId"]
        state["ultima_execucao"] = _agora_iso()
        self._save_state(state)

    def completo(self) -> None:
        query = "-in:chats"
        if not self.cfg.reprocessar:
            query += f' -label:"{self.cat.label_processado}"'
        pares = self.gmail.messages_list(query, max_results=self.cfg.max_n)
        _LOGGER.info("Completo: %d mensagem(ns) candidata(s) (query: %s).",
                     len(pares), query)
        self._processar(pares)
        state = self._load_state()
        state["historyId"] = self.gmail.get_profile()["historyId"]
        state["ultima_execucao"] = _agora_iso()
        self._save_state(state)

    def prune_logs(self, retencao_dias: int = RETENCAO_LOG_DIAS) -> None:
        """Remove entradas do decisoes.jsonl mais antigas que a retenção."""
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


# ---------------------------------------------------------------- entradas
def executar(access_token: str, cfg: MotorConfig, modo: str) -> dict:
    """Uma execução completa de triagem (chamada via executor). Retorna stats.

    Nunca levanta por LLM fora do ar: sinaliza em stats["pulado"] /
    stats["interrompido"] — semântica idêntica à CLI (incremental recupera).
    """
    gmail = GmailClient(construir_service(access_token))
    llm = LLMClient(base_url=cfg.llm_base_url, model=cfg.llm_model,
                    api_key=cfg.llm_api_key)
    if not llm.disponivel():
        _LOGGER.warning("Endpoint LLM indisponível (%s). Pulando esta execução.",
                        llm.base_url)
        return {"pulado": "llm_indisponivel"}

    cat = carregar_catalogo(os.path.join(cfg.conta_dir, "categorias.yaml"))
    motor = Motor(gmail, llm, cat, cfg)
    if not cfg.dry_run:
        motor.prune_logs()
    try:
        if modo == "completo":
            motor.completo()
        else:
            motor.incremental()
    except LLMIndisponivel as e:
        _LOGGER.warning("LLM caiu no meio (%s). O incremental recupera na "
                        "próxima rodada.", e)
        motor.stats["interrompido"] = "llm_indisponivel"
    motor.stats["ultima_execucao"] = _agora_iso()
    return motor.stats


def rodar_sugestor(access_token: str, cfg: MotorConfig, max_n: int) -> list[dict]:
    """Varre a caixa e retorna sugestões de categorias (salvas no conta_dir)."""
    from . import sugestor

    gmail = GmailClient(construir_service(access_token))
    llm = LLMClient(base_url=cfg.llm_base_url, model=cfg.llm_model,
                    api_key=cfg.llm_api_key)
    if not llm.disponivel():
        raise LLMIndisponivel(f"endpoint fora do ar: {llm.base_url}")
    cat = carregar_catalogo(os.path.join(cfg.conta_dir, "categorias.yaml"))
    metas = sugestor.amostrar(gmail, max_n)
    sugestoes = sugestor.sugerir(metas, cat, llm, log=_LOGGER)
    sugestor.salvar_json(cfg.conta_dir, sugestoes)
    return sugestoes


def aceitar_sugestoes(conta_dir: str, numeros: str) -> list[str]:
    """Aplica sugestões salvas ('1,3' ou 'todas'). Retorna nomes adicionados."""
    from . import sugestor

    sugestoes = sugestor.carregar_json(conta_dir)
    if numeros.strip().lower() in ("todas", "todos", "all"):
        aceitas = sugestoes
    else:
        idx = [int(t) for t in numeros.split(",") if t.strip().isdigit()]
        aceitas = [sugestoes[i - 1] for i in idx if 1 <= i <= len(sugestoes)]
    if aceitas:
        sugestor.aplicar_aceites(
            os.path.join(conta_dir, "categorias.yaml"), aceitas)
    return [a["nome"] for a in aceitas]


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
