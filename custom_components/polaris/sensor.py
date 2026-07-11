"""Per-account last-run sensor (state = timestamp; stats as attributes)."""
from __future__ import annotations

import datetime as dt
import json
import os

from homeassistant.components.sensor import (SensorDeviceClass,
                                             SensorStateClass, SensorEntity)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_PROGRESS, SIGNAL_RUN_DONE


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    account = hass.data[DOMAIN][entry.entry_id]
    # initial state comes from state.json (survives HA restarts)
    initial = await hass.async_add_executor_job(_read_state, account.account_dir)
    async_add_entities([LastRunSensor(account, entry, initial),
                        ProgressSensor(account, entry)])


def _read_state(account_dir: str) -> str | None:
    path = os.path.join(account_dir, "state.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f).get("last_run")
        except (json.JSONDecodeError, OSError):
            return None
    return None


class LastRunSensor(SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_has_entity_name = True
    _attr_translation_key = "last_run"
    _attr_should_poll = False

    def __init__(self, account, entry: ConfigEntry,
                 last_run_iso: str | None) -> None:
        self._account = account
        self._attr_unique_id = f"{entry.entry_id}_last_run"
        self._attr_native_value = _parse_ts(last_run_iso)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Polaris {account.email}",
            manufacturer="Polaris",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(
            self.hass, SIGNAL_RUN_DONE.format(self._account.entry.entry_id),
            self._updated))

    @callback
    def _updated(self) -> None:
        stats = self._account.last_stats or {}
        self._attr_native_value = _parse_ts(stats.get("last_run"))
        self._attr_extra_state_attributes = {
            k: v for k, v in stats.items() if k != "last_run"
        }
        self.async_write_ha_state()


class ProgressSensor(SensorEntity):
    """Live progress (%) of the current run — done/total emails.

    Updates while the run is in flight (fed from the engine through the account)
    and lands at 100% when it finishes; resets to 0 when the next run starts.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "progress"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:progress-check"
    _attr_should_poll = False

    def __init__(self, account, entry: ConfigEntry) -> None:
        self._account = account
        self._attr_unique_id = f"{entry.entry_id}_progress"
        self._attr_native_value = account.progress_pct
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Polaris {account.email}",
            manufacturer="Polaris",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(
            self.hass, SIGNAL_PROGRESS.format(self._account.entry.entry_id),
            self._updated))

    @callback
    def _updated(self) -> None:
        self._attr_native_value = self._account.progress_pct
        self._attr_extra_state_attributes = {
            "processed": self._account.progress_done,
            "total": self._account.progress_total,
        }
        self.async_write_ha_state()


def _parse_ts(iso: str | None) -> dt.datetime | None:
    if not iso:
        return None
    try:
        return dt.datetime.fromisoformat(iso)
    except ValueError:
        return None
