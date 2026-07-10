"""Plataforma application_credentials — o usuário cola client_id/secret UMA vez.

O Home Assistant guarda a credencial e conduz o OAuth inteiro (via
https://my.home-assistant.io/redirect/oauth). É o mesmo mecanismo das
integrações Google oficiais (Calendar, Mail, etc.).
"""
from __future__ import annotations

from homeassistant.components.application_credentials import AuthorizationServer
from homeassistant.core import HomeAssistant

from .const import OAUTH2_AUTHORIZE, OAUTH2_TOKEN


async def async_get_authorization_server(hass: HomeAssistant) -> AuthorizationServer:
    return AuthorizationServer(authorize_url=OAUTH2_AUTHORIZE, token_url=OAUTH2_TOKEN)


async def async_get_description_placeholders(hass: HomeAssistant) -> dict[str, str]:
    return {
        "oauth_creds_url": "https://console.cloud.google.com/apis/credentials",
        "redirect_url": "https://my.home-assistant.io/redirect/oauth",
        "docs_url": "https://github.com/Rhaiderr/polaris/blob/main/docs/gerar-credenciais-gmail.md",
    }
