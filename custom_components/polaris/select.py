"""Per-account 'Run mode' select — feeds the Run button (incremental/full)."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, MODE_FULL, MODE_INCREMENTAL


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([RunModeSelect(hass.data[DOMAIN][entry.entry_id], entry)])


class RunModeSelect(SelectEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "run_mode"
    _attr_icon = "mdi:tune-variant"
    _attr_options = [MODE_INCREMENTAL, MODE_FULL]

    def __init__(self, account, entry: ConfigEntry) -> None:
        self._account = account
        self._attr_unique_id = f"{entry.entry_id}_run_mode"
        self._attr_current_option = MODE_INCREMENTAL
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Polaris {account.email}",
            manufacturer="Polaris",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        if (last := await self.async_get_last_state()) and \
                last.state in self._attr_options:
            self._attr_current_option = last.state
        self._account.ui_mode = self._attr_current_option

    async def async_select_option(self, option: str) -> None:
        self._attr_current_option = option
        self._account.ui_mode = option
        self.async_write_ha_state()
