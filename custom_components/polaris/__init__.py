"""Polaris — Gmail triage with a local LLM, as a native HA integration.

Each Gmail account is a config entry (native OAuth, token refreshed by HA).
Triage runs through the `polaris.run_triage` service, through the daily
schedule in the options, and reports via the `polaris_run_completed` event +
a persistent notification + a last-run sensor.
"""
from __future__ import annotations

import asyncio
import logging
import os

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
    ATTR_ACCOUNT,
    ATTR_DRY_RUN,
    ATTR_MAX,
    ATTR_MODE,
    ATTR_NUMBERS,
    ATTR_REPROCESS,
    CONF_LLM_API_KEY,
    CONF_LLM_BASE_URL,
    CONF_LLM_MODEL,
    CONF_MAX_PER_RUN,
    CONF_SCHEDULE_ENABLED,
    CONF_SCHEDULE_TIME,
    CONF_SHADOW_MODE,
    CONF_USE_GMAIL_LABELS,
    DEFAULT_MAX_PER_RUN,
    DEFAULT_SCHEDULE_TIME,
    DOMAIN,
    EVENT_RUN_COMPLETED,
    MODE_FULL,
    MODE_INCREMENTAL,
    SERVICE_ACCEPT_CATEGORIES,
    SERVICE_RUN_TRIAGE,
    SERVICE_SUGGEST_CATEGORIES,
    SIGNAL_RUN_DONE,
)
from .llm_client import LLMIndisponivel

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BUTTON, Platform.NUMBER, Platform.SELECT,
             Platform.SENSOR, Platform.SWITCH]

# One run at a time across ALL accounts (equivalent to the CLI flock):
# avoids two triage runs competing for the same LLM endpoint.
_LOCK = asyncio.Lock()

SCHEMA_RUN_TRIAGE = vol.Schema({
    vol.Optional(ATTR_ACCOUNT): cv.string,
    vol.Optional(ATTR_MODE, default=MODE_INCREMENTAL):
        vol.In([MODE_INCREMENTAL, MODE_FULL]),
    vol.Optional(ATTR_MAX): cv.positive_int,
    vol.Optional(ATTR_DRY_RUN): cv.boolean,
    vol.Optional(ATTR_REPROCESS, default=False): cv.boolean,
})
SCHEMA_SUGGEST = vol.Schema({
    vol.Required(ATTR_ACCOUNT): cv.string,
    vol.Optional(ATTR_MAX, default=120): cv.positive_int,
})
SCHEMA_ACCEPT = vol.Schema({
    vol.Required(ATTR_ACCOUNT): cv.string,
    vol.Required(ATTR_NUMBERS): cv.string,
})


class PolarisAccount:
    """Per-account runtime (config entry): OAuth session, paths and schedule."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry,
                 session: config_entry_oauth2_flow.OAuth2Session) -> None:
        self.hass = hass
        self.entry = entry
        self.session = session
        self.email: str = entry.unique_id or entry.title
        self.account_dir: str = hass.config.path(DOMAIN, self.email)
        self.report_dir: str = hass.config.path("www", DOMAIN)
        self.report_token: str = ""
        self.last_stats: dict | None = None
        # state set by the UI control entities (select/switch) for the button
        self.ui_mode: str = MODE_INCREMENTAL
        self.ui_dry_run: bool = False
        self.ui_suggest_n: int = 120
        self._unsub_schedule = None

    # ------------------------------------------------------------- setup
    def prepare(self) -> None:
        """(executor) Create the account dir + initial categorias.yaml +
        the report token (unguessable filename component for the HTML report)."""
        motor.prepare_account_dir(self.account_dir)
        import secrets
        tok_path = os.path.join(self.account_dir, ".report_token")
        if os.path.exists(tok_path):
            self.report_token = open(tok_path, encoding="utf-8").read().strip()
        if not self.report_token:
            self.report_token = secrets.token_urlsafe(12)
            with open(tok_path, "w", encoding="utf-8") as f:
                f.write(self.report_token)
        os.makedirs(self.report_dir, exist_ok=True)

    def schedule(self) -> None:
        opts = self.entry.options
        if not opts.get(CONF_SCHEDULE_ENABLED):
            return
        hora = str(opts.get(CONF_SCHEDULE_TIME, DEFAULT_SCHEDULE_TIME))
        partes = hora.split(":")
        try:
            h, m = int(partes[0]), int(partes[1])
        except (ValueError, IndexError):
            _LOGGER.error("Invalid schedule time: %r", hora)
            return
        self._unsub_schedule = async_track_time_change(
            self.hass, self._scheduled, hour=h, minute=m, second=0)
        _LOGGER.info("Account %s scheduled for %02d:%02d (daily)",
                     self.email, h, m)

    @callback
    def _scheduled(self, _now) -> None:
        self.hass.async_create_task(self.async_run_triage())

    def cancel_schedule(self) -> None:
        if self._unsub_schedule:
            self._unsub_schedule()
            self._unsub_schedule = None

    # ------------------------------------------------------------- config
    def _cfg(self, dry_run: bool | None = None, max_n: int | None = None,
             reprocess: bool = False) -> motor.MotorConfig:
        o = self.entry.options
        return motor.MotorConfig(
            account_dir=self.account_dir,
            llm_base_url=o.get(CONF_LLM_BASE_URL, ""),
            llm_model=o.get(CONF_LLM_MODEL, ""),
            llm_api_key=o.get(CONF_LLM_API_KEY, ""),
            shadow_mode=o.get(CONF_SHADOW_MODE, True),
            # simulation is a per-run choice (UI switch / service arg); scheduled
            # and unspecified runs are real. Shadow mode still protects trashing.
            dry_run=bool(dry_run),
            reprocess=reprocess,
            max_n=max_n if max_n is not None
            else int(o.get(CONF_MAX_PER_RUN, DEFAULT_MAX_PER_RUN)),
            usar_labels_gmail=o.get(CONF_USE_GMAIL_LABELS, True),
            report_dir=self.report_dir,
            report_token=self.report_token,
        )

    def _endpoint_configured(self) -> bool:
        o = self.entry.options
        if o.get(CONF_LLM_BASE_URL) and o.get(CONF_LLM_MODEL):
            return True
        persistent_notification.async_create(
            self.hass,
            f"Account **{self.email}** has no model endpoint configured yet. "
            "Open the Polaris integration options and fill in the URL and "
            "the model name.",
            title="Polaris — configure the LLM endpoint",
            notification_id=f"polaris_endpoint_{self.entry.entry_id}",
        )
        return False

    async def _token(self) -> str:
        """Ensure a valid token (starts reauth if the refresh token died)."""
        try:
            await self.session.async_ensure_token_valid()
        except ClientResponseError as err:
            if err.status in (400, 401):
                self.entry.async_start_reauth(self.hass)
                raise ConfigEntryAuthFailed(
                    f"Token for account {self.email} expired") from err
            raise
        return self.session.token["access_token"]

    # ------------------------------------------------------------ triage run
    async def async_run_triage(self, mode: str = MODE_INCREMENTAL,
                               max_n: int | None = None,
                               dry_run: bool | None = None,
                               reprocess: bool = False) -> None:
        if not self._endpoint_configured():
            return
        async with _LOCK:
            token = await self._token()
            cfg = self._cfg(dry_run=dry_run, max_n=max_n, reprocess=reprocess)
            _LOGGER.info("Triage for account %s (mode=%s dry_run=%s max=%s)",
                         self.email, mode, cfg.dry_run, cfg.max_n)
            stats = await self.hass.async_add_executor_job(
                motor.executar, token, cfg, mode)

        self.last_stats = stats
        async_dispatcher_send(self.hass,
                              SIGNAL_RUN_DONE.format(self.entry.entry_id))
        self.hass.bus.async_fire(EVENT_RUN_COMPLETED,
                                 {"account": self.email, **stats})
        self._notify(stats, cfg.dry_run)

    def _notify(self, stats: dict, dry_run: bool) -> None:
        nid = f"polaris_summary_{self.entry.entry_id}"
        if stats.get("skipped_reason"):
            persistent_notification.async_create(
                self.hass,
                f"Account **{self.email}**: the model endpoint did not "
                "respond. Run skipped — the next one catches up.",
                title="Polaris — model unavailable",
                notification_id=nid)
            return
        if stats.get("bootstrap"):
            persistent_notification.async_create(
                self.hass,
                f"Account **{self.email}** initialized: the sync cursor is "
                "pinned. New emails will be triaged from now on; for the "
                "backlog, call the `polaris.run_triage` service with mode "
                "`full`.",
                title="Polaris — account initialized",
                notification_id=nid)
            return
        corpo = (
            f"Account **{self.email}**{' (dry run)' if dry_run else ''}: "
            f"{stats.get('processed', 0)} email(s) triaged — "
            f"{stats.get('label', 0)} labeled, "
            f"{stats.get('archive', 0)} archived, "
            f"{stats.get('review', 0)} in Review, "
            f"{stats.get('trash', 0)} trashed, "
            f"{stats.get('shadow', 0)} trash candidate(s)."
        )
        if stats.get("interrupted"):
            corpo += " ⚠️ The model went down mid-run; the next run continues."
        if stats.get("report_link"):
            corpo += f"\n\n📄 [Ver relatório completo]({stats['report_link']})"
        persistent_notification.async_create(
            self.hass, corpo, title="Polaris — triage summary",
            notification_id=nid)

    # ------------------------------------------------------------ suggestor
    async def async_suggest(self, max_n: int) -> None:
        if not self._endpoint_configured():
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
                    f"Account **{self.email}**: the model did not respond "
                    f"({err}).",
                    title="Polaris — suggestor",
                    notification_id=f"polaris_suggest_{self.entry.entry_id}")
                return
        if not sugestoes:
            corpo = (f"Account **{self.email}**: nothing new to suggest — "
                     "the current categories already cover the mailbox.")
        else:
            linhas = "\n".join(
                f"{i}. **{s['nome']}** (~{s['quantos']} emails) — {s['descricao']}"
                for i, s in enumerate(sugestoes, 1))
            corpo = (
                f"Suggestions for **{self.email}**:\n\n{linhas}\n\n"
                "To accept, call the `polaris.accept_categories` service "
                f"with account `{self.email}` and numbers (e.g. `1,3` or "
                "`all`)."
            )
        persistent_notification.async_create(
            self.hass, corpo, title="Polaris — category suggestions",
            notification_id=f"polaris_suggest_{self.entry.entry_id}")

    async def async_accept(self, numbers: str) -> None:
        token = await self._token()   # autonomy: create the Gmail labels now
        nomes = await self.hass.async_add_executor_job(
            motor.aceitar_sugestoes, self.account_dir, numbers, token)
        corpo = (f"Account **{self.email}**: {len(nomes)} category(ies) "
                 f"added and created in Gmail: {', '.join(nomes)}." if nomes
                 else f"Account **{self.email}**: nothing to accept.")
        persistent_notification.async_create(
            self.hass, corpo, title="Polaris — categories",
            notification_id=f"polaris_suggest_{self.entry.entry_id}")


# ---------------------------------------------------------------- services
def _accounts(hass: HomeAssistant, account: str | None) -> list[PolarisAccount]:
    todas = [d for d in hass.data.get(DOMAIN, {}).values()
             if isinstance(d, PolarisAccount)]
    if account:
        alvo = [d for d in todas if d.email == account]
        if not alvo:
            raise vol.Invalid(f"Account '{account}' not found. "
                              f"Configured: {[d.email for d in todas]}")
        return alvo
    return todas


@callback
def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_RUN_TRIAGE):
        return

    async def _run_triage(call: ServiceCall) -> None:
        for d in _accounts(hass, call.data.get(ATTR_ACCOUNT)):
            await d.async_run_triage(
                mode=call.data[ATTR_MODE],
                max_n=call.data.get(ATTR_MAX),
                dry_run=call.data.get(ATTR_DRY_RUN),
                reprocess=call.data[ATTR_REPROCESS])

    async def _suggest(call: ServiceCall) -> None:
        for d in _accounts(hass, call.data[ATTR_ACCOUNT]):
            await d.async_suggest(call.data[ATTR_MAX])

    async def _accept(call: ServiceCall) -> None:
        for d in _accounts(hass, call.data[ATTR_ACCOUNT]):
            await d.async_accept(call.data[ATTR_NUMBERS])

    hass.services.async_register(DOMAIN, SERVICE_RUN_TRIAGE, _run_triage,
                                 schema=SCHEMA_RUN_TRIAGE)
    hass.services.async_register(DOMAIN, SERVICE_SUGGEST_CATEGORIES, _suggest,
                                 schema=SCHEMA_SUGGEST)
    hass.services.async_register(DOMAIN, SERVICE_ACCEPT_CATEGORIES, _accept,
                                 schema=SCHEMA_ACCEPT)


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
            raise ConfigEntryAuthFailed("Invalid OAuth token") from err
        raise ConfigEntryNotReady from err
    except ClientError as err:
        raise ConfigEntryNotReady from err

    account = PolarisAccount(hass, entry, session)
    await hass.async_add_executor_job(account.prepare)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = account

    _register_services(hass)
    account.schedule()
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    account: PolarisAccount | None = hass.data.get(DOMAIN, {}).pop(
        entry.entry_id, None)
    if account:
        account.cancel_schedule()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
