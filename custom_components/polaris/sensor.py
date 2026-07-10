"""Sensor da última execução por conta (estado = timestamp; stats nos atributos)."""
from __future__ import annotations

import datetime as dt
import json
import os

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_EXECUCAO


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    conta = hass.data[DOMAIN][entry.entry_id]
    # estado inicial vem do state.json (sobrevive a restarts do HA)
    inicial = await hass.async_add_executor_job(_ler_state, conta.conta_dir)
    async_add_entities([UltimaExecucaoSensor(conta, entry, inicial)])


def _ler_state(conta_dir: str) -> str | None:
    path = os.path.join(conta_dir, "state.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f).get("ultima_execucao")
        except (json.JSONDecodeError, OSError):
            return None
    return None


class UltimaExecucaoSensor(SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_has_entity_name = True
    _attr_translation_key = "ultima_execucao"
    _attr_should_poll = False

    def __init__(self, conta, entry: ConfigEntry,
                 ultima_iso: str | None) -> None:
        self._conta = conta
        self._attr_unique_id = f"{entry.entry_id}_ultima_execucao"
        self._attr_native_value = _parse_ts(ultima_iso)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Polaris {conta.email}",
            manufacturer="Polaris",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(
            self.hass, SIGNAL_EXECUCAO.format(self._conta.entry.entry_id),
            self._atualizado))

    @callback
    def _atualizado(self) -> None:
        stats = self._conta.ultima_stats or {}
        self._attr_native_value = _parse_ts(stats.get("ultima_execucao"))
        self._attr_extra_state_attributes = {
            k: v for k, v in stats.items() if k != "ultima_execucao"
        }
        self.async_write_ha_state()


def _parse_ts(iso: str | None) -> dt.datetime | None:
    if not iso:
        return None
    try:
        return dt.datetime.fromisoformat(iso)
    except ValueError:
        return None
