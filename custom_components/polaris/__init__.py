"""Polaris — triagem de Gmail com LLM local, como integração nativa do HA.

Cada conta Gmail é uma config entry (OAuth nativo, token renovado pelo HA).
A triagem roda pelo serviço `polaris.executar`, pelo agendamento diário das
opções, e reporta via evento `polaris_execucao` + notificação persistente +
sensor de última execução.
"""
from __future__ import annotations

import asyncio
import logging

from aiohttp.client_exceptions import ClientError, ClientResponseError
import voluptuous as vol

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_change

from . import motor
from .const import (
    ATTR_CONTA,
    ATTR_DRY_RUN,
    ATTR_MAX,
    ATTR_MODO,
    ATTR_NUMEROS,
    ATTR_REPROCESSAR,
    CONF_AGENDAMENTO,
    CONF_DRY_RUN,
    CONF_HORA,
    CONF_LLM_API_KEY,
    CONF_LLM_BASE_URL,
    CONF_LLM_MODEL,
    CONF_MAX,
    CONF_MODO_SOMBRA,
    DEFAULT_HORA,
    DEFAULT_MAX,
    DOMAIN,
    EVENT_EXECUCAO,
    SERVICE_ACEITAR,
    SERVICE_EXECUTAR,
    SERVICE_SUGERIR,
    SIGNAL_EXECUCAO,
)
from .llm_client import LLMIndisponivel

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]

# Uma execução por vez, entre TODAS as contas (equivale ao flock da CLI):
# evita duas triagens concorrendo pelo mesmo endpoint LLM.
_LOCK = asyncio.Lock()

SCHEMA_EXECUTAR = vol.Schema({
    vol.Optional(ATTR_CONTA): cv.string,
    vol.Optional(ATTR_MODO, default="incremental"):
        vol.In(["incremental", "completo"]),
    vol.Optional(ATTR_MAX): cv.positive_int,
    vol.Optional(ATTR_DRY_RUN): cv.boolean,
    vol.Optional(ATTR_REPROCESSAR, default=False): cv.boolean,
})
SCHEMA_SUGERIR = vol.Schema({
    vol.Required(ATTR_CONTA): cv.string,
    vol.Optional(ATTR_MAX, default=120): cv.positive_int,
})
SCHEMA_ACEITAR = vol.Schema({
    vol.Required(ATTR_CONTA): cv.string,
    vol.Required(ATTR_NUMEROS): cv.string,
})


class PolarisConta:
    """Runtime de uma conta (config entry): sessão OAuth, paths e agenda."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry,
                 session: config_entry_oauth2_flow.OAuth2Session) -> None:
        self.hass = hass
        self.entry = entry
        self.session = session
        self.email: str = entry.unique_id or entry.title
        self.conta_dir: str = hass.config.path(DOMAIN, self.email)
        self.ultima_stats: dict | None = None
        self._unsub_agenda = None

    # ------------------------------------------------------------- setup
    def preparar(self) -> None:
        """(executor) Cria o diretório da conta + categorias.yaml inicial."""
        motor.preparar_conta_dir(self.conta_dir)

    def agendar(self) -> None:
        opts = self.entry.options
        if not opts.get(CONF_AGENDAMENTO):
            return
        hora = str(opts.get(CONF_HORA, DEFAULT_HORA))
        partes = hora.split(":")
        try:
            h, m = int(partes[0]), int(partes[1])
        except (ValueError, IndexError):
            _LOGGER.error("Hora de agendamento inválida: %r", hora)
            return
        self._unsub_agenda = async_track_time_change(
            self.hass, self._agendado, hour=h, minute=m, second=0)
        _LOGGER.info("Conta %s agendada para %02d:%02d (diário)",
                     self.email, h, m)

    @callback
    def _agendado(self, _now) -> None:
        self.hass.async_create_task(self.async_executar())

    def cancelar_agenda(self) -> None:
        if self._unsub_agenda:
            self._unsub_agenda()
            self._unsub_agenda = None

    # ------------------------------------------------------------- config
    def _cfg(self, dry_run: bool | None = None, max_n: int | None = None,
             reprocessar: bool = False) -> motor.MotorConfig:
        o = self.entry.options
        return motor.MotorConfig(
            conta_dir=self.conta_dir,
            llm_base_url=o.get(CONF_LLM_BASE_URL, ""),
            llm_model=o.get(CONF_LLM_MODEL, ""),
            llm_api_key=o.get(CONF_LLM_API_KEY, ""),
            modo_sombra=o.get(CONF_MODO_SOMBRA, True),
            dry_run=o.get(CONF_DRY_RUN, False) if dry_run is None else dry_run,
            reprocessar=reprocessar,
            max_n=max_n if max_n is not None else int(o.get(CONF_MAX, DEFAULT_MAX)),
        )

    def _endpoint_configurado(self) -> bool:
        o = self.entry.options
        if o.get(CONF_LLM_BASE_URL) and o.get(CONF_LLM_MODEL):
            return True
        persistent_notification.async_create(
            self.hass,
            f"A conta **{self.email}** ainda não tem o endpoint do modelo "
            "configurado. Abra as opções da integração Polaris e preencha "
            "a URL e o modelo.",
            title="Polaris — configure o endpoint do LLM",
            notification_id=f"polaris_endpoint_{self.entry.entry_id}",
        )
        return False

    async def _token(self) -> str:
        """Garante token válido (dispara reauth se o refresh morreu)."""
        try:
            await self.session.async_ensure_token_valid()
        except ClientResponseError as err:
            if err.status in (400, 401):
                self.entry.async_start_reauth(self.hass)
                raise ConfigEntryAuthFailed(
                    f"Token da conta {self.email} expirou") from err
            raise
        return self.session.token["access_token"]

    # ------------------------------------------------------------ execução
    async def async_executar(self, modo: str = "incremental",
                             max_n: int | None = None,
                             dry_run: bool | None = None,
                             reprocessar: bool = False) -> None:
        if not self._endpoint_configurado():
            return
        async with _LOCK:
            token = await self._token()
            cfg = self._cfg(dry_run=dry_run, max_n=max_n,
                            reprocessar=reprocessar)
            _LOGGER.info("Triagem da conta %s (modo=%s dry_run=%s max=%s)",
                         self.email, modo, cfg.dry_run, cfg.max_n)
            stats = await self.hass.async_add_executor_job(
                motor.executar, token, cfg, modo)

        self.ultima_stats = stats
        async_dispatcher_send(self.hass,
                              SIGNAL_EXECUCAO.format(self.entry.entry_id))
        self.hass.bus.async_fire(EVENT_EXECUCAO,
                                 {"conta": self.email, **stats})
        self._notificar(stats, cfg.dry_run)

    def _notificar(self, stats: dict, dry_run: bool) -> None:
        nid = f"polaris_resumo_{self.entry.entry_id}"
        if stats.get("pulado"):
            persistent_notification.async_create(
                self.hass,
                f"Conta **{self.email}**: o endpoint do modelo não respondeu. "
                "Execução pulada — a próxima recupera o atraso.",
                title="Polaris — modelo indisponível",
                notification_id=nid)
            return
        if stats.get("bootstrap"):
            persistent_notification.async_create(
                self.hass,
                f"Conta **{self.email}** inicializada: o cursor de "
                "sincronização foi fixado. Novos emails serão triados a "
                "partir de agora; para o backlog, chame o serviço "
                "`polaris.executar` com modo `completo`.",
                title="Polaris — conta inicializada",
                notification_id=nid)
            return
        corpo = (
            f"Conta **{self.email}**{' (simulação)' if dry_run else ''}: "
            f"{stats.get('processados', 0)} email(s) triado(s) — "
            f"{stats.get('label', 0)} rotulado(s), "
            f"{stats.get('arquivar', 0)} arquivado(s), "
            f"{stats.get('revisar', 0)} em Revisar, "
            f"{stats.get('excluir', 0)} na Lixeira, "
            f"{stats.get('sombra', 0)} candidato(s) à Lixeira."
        )
        if stats.get("interrompido"):
            corpo += " ⚠️ O modelo caiu no meio; a próxima execução continua."
        persistent_notification.async_create(
            self.hass, corpo, title="Polaris — resumo da triagem",
            notification_id=nid)

    # ------------------------------------------------------------ sugestor
    async def async_sugerir(self, max_n: int) -> None:
        if not self._endpoint_configurado():
            return
        async with _LOCK:
            token = await self._token()
            cfg = self._cfg()
            try:
                sugestoes = await self.hass.async_add_executor_job(
                    motor.rodar_sugestor, token, cfg, max_n)
            except LLMIndisponivel as err:
                persistent_notification.async_create(
                    self.hass,
                    f"Conta **{self.email}**: o modelo não respondeu ({err}).",
                    title="Polaris — sugestor",
                    notification_id=f"polaris_sugestoes_{self.entry.entry_id}")
                return
        if not sugestoes:
            corpo = (f"Conta **{self.email}**: nenhuma categoria nova a "
                     "sugerir — as atuais já cobrem a caixa.")
        else:
            linhas = "\n".join(
                f"{i}. **{s['nome']}** (~{s['quantos']} emails) — {s['descricao']}"
                for i, s in enumerate(sugestoes, 1))
            corpo = (
                f"Sugestões para **{self.email}**:\n\n{linhas}\n\n"
                "Para aceitar, chame o serviço `polaris.aceitar_categorias` "
                f"com conta `{self.email}` e números (ex.: `1,3` ou `todas`)."
            )
        persistent_notification.async_create(
            self.hass, corpo, title="Polaris — sugestões de categorias",
            notification_id=f"polaris_sugestoes_{self.entry.entry_id}")

    async def async_aceitar(self, numeros: str) -> None:
        nomes = await self.hass.async_add_executor_job(
            motor.aceitar_sugestoes, self.conta_dir, numeros)
        corpo = (f"Conta **{self.email}**: {len(nomes)} categoria(s) "
                 f"adicionada(s): {', '.join(nomes)}." if nomes
                 else f"Conta **{self.email}**: nada a aceitar.")
        persistent_notification.async_create(
            self.hass, corpo, title="Polaris — categorias",
            notification_id=f"polaris_sugestoes_{self.entry.entry_id}")


# ---------------------------------------------------------------- serviços
def _contas(hass: HomeAssistant, conta: str | None) -> list[PolarisConta]:
    todas = [d for d in hass.data.get(DOMAIN, {}).values()
             if isinstance(d, PolarisConta)]
    if conta:
        alvo = [d for d in todas if d.email == conta]
        if not alvo:
            raise vol.Invalid(f"Conta '{conta}' não encontrada. "
                              f"Configuradas: {[d.email for d in todas]}")
        return alvo
    return todas


@callback
def _registrar_servicos(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_EXECUTAR):
        return

    async def _executar(call: ServiceCall) -> None:
        for d in _contas(hass, call.data.get(ATTR_CONTA)):
            await d.async_executar(
                modo=call.data[ATTR_MODO],
                max_n=call.data.get(ATTR_MAX),
                dry_run=call.data.get(ATTR_DRY_RUN),
                reprocessar=call.data[ATTR_REPROCESSAR])

    async def _sugerir(call: ServiceCall) -> None:
        for d in _contas(hass, call.data[ATTR_CONTA]):
            await d.async_sugerir(call.data[ATTR_MAX])

    async def _aceitar(call: ServiceCall) -> None:
        for d in _contas(hass, call.data[ATTR_CONTA]):
            await d.async_aceitar(call.data[ATTR_NUMEROS])

    hass.services.async_register(DOMAIN, SERVICE_EXECUTAR, _executar,
                                 schema=SCHEMA_EXECUTAR)
    hass.services.async_register(DOMAIN, SERVICE_SUGERIR, _sugerir,
                                 schema=SCHEMA_SUGERIR)
    hass.services.async_register(DOMAIN, SERVICE_ACEITAR, _aceitar,
                                 schema=SCHEMA_ACEITAR)


# ------------------------------------------------------------- entry setup
async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    implementation = (
        await config_entry_oauth2_flow.async_get_config_entry_implementation(
            hass, entry))
    session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)
    try:
        await session.async_ensure_token_valid()
    except ClientResponseError as err:
        if err.status in (400, 401):
            raise ConfigEntryAuthFailed("Token OAuth inválido") from err
        raise ConfigEntryNotReady from err
    except ClientError as err:
        raise ConfigEntryNotReady from err

    conta = PolarisConta(hass, entry, session)
    await hass.async_add_executor_job(conta.preparar)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = conta

    _registrar_servicos(hass)
    conta.agendar()
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    conta: PolarisConta | None = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if conta:
        conta.cancelar_agenda()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
