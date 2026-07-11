"""Polaris config flow — HA-native OAuth ("Sign in with Google").

Same pattern as the official Google integrations: the user registers the
credential (client_id/secret of a *Web*-type OAuth app) once in Application
Credentials, and each Gmail account becomes a config entry ("Add integration
→ Polaris → approve on Google → done"). HA stores the token and refreshes it
by itself. Multi-account = add the integration again.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow, selector

from .const import (
    CONF_LLM_API_KEY,
    CONF_LLM_BASE_URL,
    CONF_LLM_MODEL,
    CONF_MAX_PER_RUN,
    CONF_SCHEDULE_ENABLED,
    CONF_SCHEDULE_TIME,
    CONF_SHADOW_MODE,
    DEFAULT_MAX_PER_RUN,
    DEFAULT_SCHEDULE_TIME,
    DOMAIN,
    SCOPE_GMAIL,
)

_LOGGER = logging.getLogger(__name__)


class PolarisFlowHandler(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """OAuth via my.home-assistant.io — no error page, no URL pasting."""

    DOMAIN = DOMAIN
    VERSION = 1

    @property
    def logger(self) -> logging.Logger:
        return _LOGGER

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        # offline + consent guarantee a refresh_token (login never expires).
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
        """Token obtained — resolve the email (entry title/unique_id)."""

        def _profile() -> dict:
            from .gmail_client import GmailClient

            return GmailClient(data["token"]["access_token"]).get_profile()

        try:
            profile = await self.hass.async_add_executor_job(_profile)
        except Exception:  # noqa: BLE001 — any failure here = cannot connect
            _LOGGER.exception("Failed to query the Gmail profile")
            return self.async_abort(reason="cannot_connect")

        email = profile["emailAddress"]
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
    """Per-account options: LLM endpoint, schedule and behavior."""

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
                    CONF_SCHEDULE_ENABLED,
                    default=o.get(CONF_SCHEDULE_ENABLED, False)
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_SCHEDULE_TIME,
                    default=o.get(CONF_SCHEDULE_TIME, DEFAULT_SCHEDULE_TIME)
                ): selector.TimeSelector(),
                vol.Required(
                    CONF_MAX_PER_RUN,
                    default=o.get(CONF_MAX_PER_RUN, DEFAULT_MAX_PER_RUN)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=500, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Required(
                    CONF_SHADOW_MODE, default=o.get(CONF_SHADOW_MODE, True)
                ): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
