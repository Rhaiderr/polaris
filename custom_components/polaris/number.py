"""Per-account 'Sample size for suggestions' number — feeds the Suggest button."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
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
    async_add_entities([SuggestSampleNumber(hass.data[DOMAIN][entry.entry_id], entry)])


class SuggestSampleNumber(NumberEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "suggest_sample"
    _attr_icon = "mdi:counter"
    _attr_native_min_value = 20
    _attr_native_max_value = 1000
    _attr_native_step = 20
    _attr_mode = NumberMode.BOX

    def __init__(self, account, entry: ConfigEntry) -> None:
        self._account = account
        self._attr_unique_id = f"{entry.entry_id}_suggest_sample"
        self._attr_native_value = 120
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Polaris {account.email}",
            manufacturer="Polaris",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        if (last := await self.async_get_last_state()) is not None:
            try:
                self._attr_native_value = float(last.state)
            except (TypeError, ValueError):
                pass
        self._account.ui_suggest_n = int(self._attr_native_value)

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self._account.ui_suggest_n = int(value)
        self.async_write_ha_state()
