"""Buttons to trigger a triage run on demand, per account."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MODE_FULL, MODE_INCREMENTAL


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    account = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        RunTriageButton(account, entry, MODE_INCREMENTAL, "mdi:email-sync-outline"),
        RunTriageButton(account, entry, MODE_FULL, "mdi:email-search-outline"),
    ])


class RunTriageButton(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, account, entry: ConfigEntry, mode: str, icon: str) -> None:
        self._account = account
        self._mode = mode
        self._attr_icon = icon
        self._attr_unique_id = f"{entry.entry_id}_run_{mode}"
        self._attr_translation_key = f"run_{mode}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Polaris {account.email}",
            manufacturer="Polaris",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_press(self) -> None:
        await self._account.async_run_triage(mode=self._mode)
