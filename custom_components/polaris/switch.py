"""Per-account 'Simulation (dry-run)' switch — feeds the Run button.

When on, pressing the Run button previews the triage without touching Gmail.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([SimulationSwitch(hass.data[DOMAIN][entry.entry_id], entry)])


class SimulationSwitch(SwitchEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "dry_run"
    _attr_icon = "mdi:flask-outline"
    _attr_entity_category = None

    def __init__(self, account, entry: ConfigEntry) -> None:
        self._account = account
        self._attr_unique_id = f"{entry.entry_id}_dry_run"
        self._attr_is_on = False
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Polaris {account.email}",
            manufacturer="Polaris",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        if (last := await self.async_get_last_state()) is not None:
            self._attr_is_on = last.state == "on"
        self._account.ui_dry_run = self._attr_is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._attr_is_on = True
        self._account.ui_dry_run = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._attr_is_on = False
        self._account.ui_dry_run = False
        self.async_write_ha_state()
