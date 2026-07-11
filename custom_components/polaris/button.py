"""Button to trigger a triage run on demand, per account.

Runs with the mode chosen in the account's "Run mode" select and the
"Simulation" switch — everything from the device page, no Developer Tools.
"""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    account = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([RunButton(account, entry)])


class RunButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "run"
    _attr_icon = "mdi:email-fast-outline"

    def __init__(self, account, entry: ConfigEntry) -> None:
        self._account = account
        self._attr_unique_id = f"{entry.entry_id}_run"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Polaris {account.email}",
            manufacturer="Polaris",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_press(self) -> None:
        await self._account.async_run_triage(
            mode=self._account.ui_mode, dry_run=self._account.ui_dry_run)
