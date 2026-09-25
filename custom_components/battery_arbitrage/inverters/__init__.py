"""Inverter backends: FoxESS (foxess_modbus) and solax_modbus.

Imports are lazy so the pure profile modules can be imported (and unit
tested) without Home Assistant installed.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from ..const import (
    CONF_INVERTER_BACKEND,
    DEFAULT_INVERTER_BACKEND,
    INVERTER_BACKEND_SOLAX,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from ..actuation import Actuator
    from .base import InverterBackend


def build_backend(
    hass: HomeAssistant,
    config: dict[str, Any],
    actuator: Actuator,
    store: Callable[[], dict[str, Any]],
    save: Callable[[], None],
) -> InverterBackend:
    """Instantiate the backend selected in the config entry."""
    backend = config.get(CONF_INVERTER_BACKEND, DEFAULT_INVERTER_BACKEND)
    if backend == INVERTER_BACKEND_SOLAX:
        from .solax_modbus import SolaxModbusBackend  # noqa: PLC0415
        return SolaxModbusBackend(hass, config, actuator, store, save)
    from .foxess import FoxessBackend  # noqa: PLC0415
    return FoxessBackend(hass, config, actuator, store, save)
