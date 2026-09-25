"""Unit normalisation for power and energy sensor readings.

Solar AI's control math works in kW and kWh. FoxESS Modbus reports those
units directly, but other inverter integrations (e.g. solax_modbus for
Growatt) report W and Wh. These helpers convert a raw state value using the
entity's `unit_of_measurement` attribute. A missing or unknown unit is
treated as already being kW / kWh, which keeps the historical behaviour for
FoxESS installs unchanged.

Pure functions, no Home Assistant imports — easy to unit test.
"""
from __future__ import annotations

_POWER_TO_KW = {
    "w": 0.001,
    "kw": 1.0,
    "mw": 1000.0,
}

_ENERGY_TO_KWH = {
    "wh": 0.001,
    "kwh": 1.0,
    "mwh": 1000.0,
}


def power_to_kw(value: float, unit: str | None) -> float:
    """Convert a power reading to kW. Unknown/missing units pass through."""
    factor = _POWER_TO_KW.get((unit or "").strip().lower(), 1.0)
    return float(value) * factor


def energy_to_kwh(value: float, unit: str | None) -> float:
    """Convert an energy reading to kWh. Unknown/missing units pass through."""
    factor = _ENERGY_TO_KWH.get((unit or "").strip().lower(), 1.0)
    return float(value) * factor
