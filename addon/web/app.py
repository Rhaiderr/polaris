"""Wizard de onboarding do Polaris (front do add-on Home Assistant).

Não reimplementa nada do núcleo: orquestra a CLI existente
(`python -m src.orquestrador ...`) por subprocess e consome os JSONs que ela já
produz (logs/<conta>/sugestoes.json). As 4 telas seguem o roadmap da Fase 6:

  1. /conta      — vincular conta Google (status + provisão de token)
  2. /endpoint   — configurar o endpoint do LLM + testar conexão
  3. /categorias — varredura + sugestões de categorias (checkboxes)
  4. /agendar    — horário da execução diária (scheduler interno)

Roda tanto no container do add-on (via gunicorn, atrás do ingress do HA) quanto
localmente para desenvolvimento (`python addon/web/app.py`).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

from flask import (Flask, jsonify, redirect, render_template, request, url_for)

# --- torna o núcleo (src/) importável, seja no container (/app) ou localmente.
_AQUI = os.path.dirname(os.path.abspath(__file__))
_RAIZ = os.path.dirname(os.path.dirname(_AQUI))   # .../polaris
if _RAIZ not in sys.path:
    sys.path.insert(0, _RAIZ)

from src import orquestrador as orq          # noqa: E402
from src import sugestor                       # noqa: E402
from src.gmail_client import CREDENTIALS_PATH  # noqa: E402
from src.llm_client import LLMClient           # noqa: E402

BASE_DIR = orq.BASE_DIR
CONFIG_DIR = orq.CONFIG_DIR
LOGS_DIR = orq.LOGS_DIR
ENV_PATH = orq.ENV_PATH

# Chaves editáveis pela Tela 2 (na ordem em que aparecem no formulário).
ENV_KEYS = ["LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "MODO_SOMBRA_EXCLUSAO"]


# ----------------------------------------------------------------- ingress
class IngressMiddleware:
    """O HA serve o add-on sob um prefixo dinâmico e o envia no cabeçalho
    `X-Ingress-Path`. Copiamos para SCRIPT_NAME para o url_for gerar links
    corretos. Sem o cabeçalho (execução local), o app fica na raiz."""

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        prefixo = environ.get("HTTP_X_INGRESS_PATH", "")
        if prefixo:
            environ["SCRIPT_NAME"] = prefixo
        return self.app(environ, start_response)


# ----------------------------------------------------------------- .env I/O
def ler_env() -> dict:
    valores = {k: "" for k in ENV_KEYS}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            for linha in f:
                linha = linha.strip()
                if not linha or linha.startswith("#") or "=" not in linha:
                    continue
                chave, _, valor = linha.partition("=")
                valores[chave.strip()] = valor.strip()
    return valores


def escrever_env(novos: dict) -> None:
    """Reescreve o .env preservando chaves não gerenciadas por esta tela."""
    atuais = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            for linha in f:
                s = linha.strip()
                if s and not s.startswith("#") and "=" in s:
                    k, _, v = s.partition("=")
                    atuais[k.strip()] = v.strip()
    atuais.update({k: v for k, v in novos.items() if v is not None})
    tmp = ENV_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for k, v in atuais.items():
            f.write(f"{k}={v}\n")
    os.replace(tmp, ENV_PATH)
    # reflete imediatamente no processo (para o botão "testar" logo em seguida)
    for k, v in atuais.items():
        os.environ[k] = v


# ----------------------------------------------- runner de jobs em background
class Job:
    """Uma tarefa longa por vez (varredura de sugestões, execução da triagem,
    login). A UI dispara e faz polling do status + tail do log."""

    def __init__(self):
        self.lock = threading.Lock()
        self.nome: str | None = None
        self.proc: subprocess.Popen | None = None
        self.log_path: str | None = None
        self.iniciado_em: float | None = None

    def rodando(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def iniciar(self, nome: str, args: list[str]) -> tuple[bool, str]:
        with self.lock:
            if self.rodando():
                return False, f"Já existe um job em andamento: {self.nome}"
            os.makedirs(LOGS_DIR, exist_ok=True)
            self.log_path = os.path.join(LOGS_DIR, "wizard-job.log")
            log = open(self.log_path, "w", encoding="utf-8")
            log.write(f"$ {' '.join(args)}\n\n")
            log.flush()
            env = dict(os.environ, PYTHONUNBUFFERED="1")
            self.proc = subprocess.Popen(
                [sys.executable, "-u", "-m", "src.orquestrador", *args],
                cwd=BASE_DIR, env=env, stdout=log, stderr=subprocess.STDOUT,
            )
            self.nome = nome
            self.iniciado_em = time.time()
            return True, "iniciado"

    def status(self) -> dict:
        rodando = self.rodando()
        rc = None if rodando or self.proc is None else self.proc.returncode
        saida = ""
        if self.log_path and os.path.exists(self.log_path):
            with open(self.log_path, encoding="utf-8", errors="replace") as f:
                saida = f.read()[-6000:]
        return {"nome": self.nome, "rodando": rodando,
                "returncode": rc, "log": saida}


JOB = Job()


# ------------------------------------------------------------- estado/contas
def contas_status() -> list[dict]:
    out = []
    for conta in orq.perfis_configurados():
        cdir = orq.conta_dir(conta)
        cats = 0
        cat_path = os.path.join(cdir, "categorias.yaml")
        if os.path.exists(cat_path):
            try:
                import yaml
                with open(cat_path, encoding="utf-8") as f:
                    cats = len((yaml.safe_load(f) or {}).get("categorias", []))
            except Exception:
                cats = 0
        sug = os.path.join(LOGS_DIR, conta, "sugestoes.json")
        out.append({
            "nome": conta,
            "tem_token": os.path.exists(os.path.join(cdir, "token.json")),
            "categorias": cats,
            "tem_sugestoes": os.path.exists(sug),
        })
    return out


def _passos(ativo: str) -> list[dict]:
    itens = [("conta", "1. Conta Google"), ("endpoint", "2. Endpoint do LLM"),
             ("categorias", "3. Categorias"), ("agendar", "4. Agendar")]
    return [{"slug": s, "rotulo": r, "ativo": s == ativo} for s, r in itens]


# ------------------------------------------------------------------ app
def criar_app() -> Flask:
    app = Flask(__name__)
    app.wsgi_app = IngressMiddleware(app.wsgi_app)
    orq._carregar_dotenv()   # carrega .env para os.environ

    @app.context_processor
    def _globais():
        return {"contas": contas_status()}

    # ---- dashboard
    @app.route("/")
    def index():
        return render_template("index.html", passos=_passos(""),
                               credenciais_ok=os.path.exists(CREDENTIALS_PATH))

    # ---- Tela 1: conta Google
    @app.route("/conta")
    def conta():
        return render_template("conta.html", passos=_passos("conta"),
                               credenciais_ok=os.path.exists(CREDENTIALS_PATH),
                               oauth_port=os.environ.get("OAUTH_PORT", "8765"))

    # ---- Tela 2: endpoint do LLM
    @app.route("/endpoint", methods=["GET", "POST"])
    def endpoint():
        if request.method == "POST":
            escrever_env({
                "LLM_BASE_URL": request.form.get("llm_base_url", "").strip(),
                "LLM_MODEL": request.form.get("llm_model", "").strip(),
                "LLM_API_KEY": request.form.get("llm_api_key", "").strip(),
            })
            return redirect(url_for("endpoint", salvo="1"))
        return render_template("endpoint.html", passos=_passos("endpoint"),
                               env=ler_env(), salvo=request.args.get("salvo"))

    @app.route("/endpoint/testar", methods=["POST"])
    def endpoint_testar():
        d = request.get_json(silent=True) or {}
        try:
            cli = LLMClient(base_url=d.get("llm_base_url", "").strip(),
                            model=d.get("llm_model", "").strip(),
                            api_key=d.get("llm_api_key", "").strip())
        except ValueError as e:
            return jsonify(ok=False, msg=str(e))
        return jsonify(ok=cli.disponivel(),
                       msg="Conexão OK" if cli.disponivel()
                           else "Sem resposta do endpoint")

    # ---- Tela 3: categorias
    @app.route("/categorias")
    def categorias():
        conta_sel = request.args.get("conta") or (
            orq.perfis_configurados()[0] if orq.perfis_configurados() else "")
        sugestoes = []
        if conta_sel:
            try:
                sugestoes = sugestor.carregar_json(conta_sel, LOGS_DIR)
            except FileNotFoundError:
                sugestoes = []
        return render_template("categorias.html", passos=_passos("categorias"),
                               conta_sel=conta_sel, sugestoes=sugestoes)

    @app.route("/categorias/varrer", methods=["POST"])
    def categorias_varrer():
        conta_sel = request.form.get("conta", "").strip()
        maxn = request.form.get("max", "120").strip() or "120"
        ok, msg = JOB.iniciar(
            f"Sugestões · {conta_sel}",
            ["--conta", conta_sel, "--sugerir-categorias", "--max", maxn])
        return jsonify(ok=ok, msg=msg)

    @app.route("/categorias/aceitar", methods=["POST"])
    def categorias_aceitar():
        conta_sel = request.form.get("conta", "").strip()
        nums = request.form.getlist("aceitar")
        if not nums:
            return redirect(url_for("categorias", conta=conta_sel))
        ok, msg = JOB.iniciar(
            f"Aceite · {conta_sel}",
            ["--conta", conta_sel, "--aceitar", ",".join(nums)])
        return redirect(url_for("categorias", conta=conta_sel, aceitando="1"))

    # ---- Tela 4: agendar
    @app.route("/agendar", methods=["GET", "POST"])
    def agendar():
        cfg = ler_agenda()
        if request.method == "POST":
            cfg = {"ativo": request.form.get("ativo") == "on",
                   "hora": request.form.get("hora", "07:00")}
            escrever_agenda(cfg)
            return redirect(url_for("agendar", salvo="1"))
        return render_template("agendar.html", passos=_passos("agendar"),
                               agenda=cfg, salvo=request.args.get("salvo"))

    # ---- execução manual + status de job (usado por polling da UI)
    @app.route("/rodar", methods=["POST"])
    def rodar():
        dry = request.form.get("dry_run") == "on"
        args = ["--modo", "completo"]
        if dry:
            args.append("--dry-run")
        ok, msg = JOB.iniciar("Triagem (todas as contas)", args)
        return jsonify(ok=ok, msg=msg)

    @app.route("/job/status")
    def job_status():
        return jsonify(JOB.status())

    return app


# ---------------------------------------------------------------- agenda
def _agenda_path() -> str:
    return os.path.join(CONFIG_DIR, "agenda.json")


def ler_agenda() -> dict:
    p = _agenda_path()
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"ativo": False, "hora": "07:00"}


def escrever_agenda(cfg: dict) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = _agenda_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _agenda_path())


# --------------------------------------------------- scheduler interno
def _loop_agenda():
    """Dispara a triagem de TODAS as contas no horário configurado (1x/dia)."""
    ultimo_disparo = None
    while True:
        try:
            cfg = ler_agenda()
            agora = datetime.now()
            marca = agora.strftime("%Y-%m-%d %H:%M")
            if (cfg.get("ativo") and agora.strftime("%H:%M") == cfg.get("hora")
                    and ultimo_disparo != marca and not JOB.rodando()):
                ultimo_disparo = marca
                JOB.iniciar("Triagem agendada", ["--modo", "completo"])
        except Exception:
            pass
        time.sleep(30)


def iniciar_scheduler():
    t = threading.Thread(target=_loop_agenda, daemon=True)
    t.start()


app = criar_app()
iniciar_scheduler()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8099")), debug=True)
