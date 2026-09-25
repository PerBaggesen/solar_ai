"""SolaX profile for solax_modbus (plugin "solax") — sensing only for now.

Control is not implemented yet. The intended mechanism is SolaX "Mode 1"
remote control (solax_modbus `plugin_solax.py`):

  * `remotecontrol_power_control` select → "Enabled Power Control"
  * `remotecontrol_active_power` number (W, S32): positive = import/charge,
    negative = export/discharge
  * `remotecontrol_autorepeat_duration` number (s) — solax_modbus re-sends
    the command every poll until this expires, then writes "Disabled"
  * press `remotecontrol_trigger` to send (the numbers/selects above are
    local-only until the button is pressed)
  * self use: set power control "Disabled" and press the trigger again
  * min SoC: `selfuse_discharge_min_soc`; export cap:
    `export_control_user_limit` (W)

Implementing it means filling in the plan_* methods below, marking
`control_supported = True`, and verifying on hardware.
"""
from __future__ import annotations

from ...const import (
    CONF_BATTERY_SOC_ENTITY,
    CONF_FOXESS_GRID_EXPORT_ENTITY,
    CONF_FOXESS_GRID_IMPORT_ENTITY,
    CONF_FOXESS_LOAD_POWER_ENTITY,
    CONF_FOXESS_PV_POWER_ENTITY,
)
from .base import PluginProfile


class SolaxProfile(PluginProfile):
    plugin = "solax"
    label = "SolaX (monitoring only — control not implemented yet)"
    control_supported = False

    # SolaX reports battery power as one signed sensor (`battery_power_charge`,
    # + = charge), which Solar AI's separate charge/discharge inputs can't use
    # directly — those two are left for the user to map (e.g. via templates).
    sensor_keys = {
        CONF_BATTERY_SOC_ENTITY: ("battery_capacity",),
        CONF_FOXESS_GRID_IMPORT_ENTITY: ("grid_import",),
        CONF_FOXESS_GRID_EXPORT_ENTITY: ("grid_export",),
        CONF_FOXESS_PV_POWER_ENTITY: ("pv_power_total",),
        CONF_FOXESS_LOAD_POWER_ENTITY: ("house_load",),
    }
