"""Config flow do Polaris — OAuth nativo do HA ("Entrar com Google").

O fluxo é o padrão das integrações Google oficiais: o usuário registra a
credencial (client_id/secret de um app OAuth tipo *Web*) uma única vez em
Application Credentials, e cada conta Gmail vira uma config entry ("Adicionar
integração → Polaris → aprovar no Google → pronto"). O HA guarda o token e
renova sozinho. Multi-conta = adicionar a integração de novo.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow, selector

from .const import (
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
    SCOPE_GMAIL,
)

_LOGGER = logging.getLogger(__name__)


class PolarisFlowHandler(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """OAuth via my.home-assistant.io — sem tela de erro, sem colar URL."""

    DOMAIN = DOMAIN
    VERSION = 1

    @property
    def logger(self) -> logging.Logger:
        return _LOGGER

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        # offline + consent garantem o refresh_token (login que não expira).
        return {
            "scope": SCOPE_GMAIL,
            "access_type": "offline",
            "prompt": "consent",
        }

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_user()

    async def async_oauth_create_entry(
        self, data: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        """Token obtido — descobre o email (vira o nome/unique_id da entry)."""

        def _perfil() -> dict:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            creds = Credentials(token=data["token"]["access_token"])
            service = build("gmail", "v1", credentials=creds, cache_discovery=False)
            return service.users().getProfile(userId="me").execute()

        try:
            perfil = await self.hass.async_add_executor_job(_perfil)
        except Exception:  # noqa: BLE001 — qualquer falha aqui = não conectou
            _LOGGER.exception("Falha ao consultar o perfil do Gmail")
            return self.async_abort(reason="cannot_connect")

        email = perfil["emailAddress"]
        await self.async_set_unique_id(email)

        if self.source == config_entries.SOURCE_REAUTH:
            entry = self.hass.config_entries.async_get_entry(
                self.context["entry_id"]
            )
            if entry:
                self.hass.config_entries.async_update_entry(entry, data=data)
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=email, data=data)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> PolarisOptionsFlow:
        return PolarisOptionsFlow()


class PolarisOptionsFlow(config_entries.OptionsFlow):
    """Opções por conta: endpoint do LLM, agendamento e comportamento."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        o = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_LLM_BASE_URL, default=o.get(CONF_LLM_BASE_URL, "")
                ): selector.TextSelector(),
                vol.Required(
                    CONF_LLM_MODEL, default=o.get(CONF_LLM_MODEL, "")
                ): selector.TextSelector(),
                vol.Optional(
                    CONF_LLM_API_KEY, default=o.get(CONF_LLM_API_KEY, "")
                ): selector.TextSelector(
                    selector.TextSelectorConfig(
                        type=selector.TextSelectorType.PASSWORD
                    )
                ),
                vol.Required(
                    CONF_AGENDAMENTO, default=o.get(CONF_AGENDAMENTO, False)
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_HORA, default=o.get(CONF_HORA, DEFAULT_HORA)
                ): selector.TimeSelector(),
                vol.Required(
                    CONF_MAX, default=o.get(CONF_MAX, DEFAULT_MAX)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=500, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Required(
                    CONF_MODO_SOMBRA, default=o.get(CONF_MODO_SOMBRA, True)
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_DRY_RUN, default=o.get(CONF_DRY_RUN, False)
                ): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
