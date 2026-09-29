"""Growatt profile for solax_modbus (plugin "growatt"), using VPP remote control.

Growatt GEN3/GEN4 hybrids expose a "VPP" power-control block (solax_modbus
`plugin_growatt.py`):

    vpp_status            select  reg 30100  Disabled / Enabled
    vpp_remote_control    select  reg 30407  Disabled / Enabled
    vpp_time              number  reg 30408  0–1440 min (step 5)
    vpp_power             number  reg 30409  −100…100 % of rated power (step 5)
    vpp_allow_ac_charging select  reg 30410  Disabled / Enabled

Remote control holds the battery at `vpp_power` for `vpp_time` minutes, then
the inverter drops back to its own EMS by itself — a hardware watchdog.
Solar AI keeps a forced mode alive by re-sending the command periodically, so
if Solar AI (or HA) stalls, the inverter recovers on its own.

The sign of `vpp_power` (charge = positive?) is not documented in
solax_modbus. It is therefore a *verified* value: the commissioning self-test
measures the battery response and stores the sign; control stays blocked
until it has.
"""
from __future__ import annotations

from ...const import (
    CONF_BATTERY_CHARGE_ENTITY,
    CONF_BATTERY_CHARGE_TOTAL_ENTITY,
    CONF_BATTERY_DISCHARGE_ENTITY,
    CONF_BATTERY_DISCHARGE_TOTAL_ENTITY,
    CONF_BATTERY_SOC_ENTITY,
    CONF_CELL_TEMP_ENTITY,
    CONF_FOXESS_GRID_EXPORT_ENTITY,
    CONF_FOXESS_GRID_IMPORT_ENTITY,
    CONF_FOXESS_LOAD_POWER_ENTITY,
    CONF_FOXESS_PV_POWER_ENTITY,
)
from .base import (
    INTENT_FORCE_CHARGE,
    INTENT_FORCE_DISCHARGE,
    PLATFORM_NUMBER,
    PLATFORM_SELECT,
    PluginProfile,
    ProfileContext,
    Write,
)

ENABLED = "Enabled"
DISABLED = "Disabled"

KEY_VPP_STATUS = "vpp_status"
KEY_VPP_REMOTE = "vpp_remote_control"
KEY_VPP_TIME = "vpp_time"
KEY_VPP_POWER = "vpp_power"
KEY_VPP_AC_CHARGE = "vpp_allow_ac_charging"
KEY_LIMIT_GRID_EXPORT = "limit_grid_export"
KEY_GRID_EXPORT_LIMIT = "grid_export_limit"

VPP_POWER_STEP = 5
VPP_TIME_STEP = 5


def kw_to_vpp_pct(kw: float, rated_kw: float, max_pct: int = 100) -> int:
    """Convert a power request to a VPP percentage (unsigned).

    Rounds to the register's 5 % step, never rounds a real request down to
    0 (that would silently turn "charge slowly" into "hold"), and clamps to
    `max_pct`.
    """
    if kw <= 0 or rated_kw <= 0:
        return 0
    pct = round(kw / rated_kw * 100 / VPP_POWER_STEP) * VPP_POWER_STEP
    pct = max(VPP_POWER_STEP, pct)
    cap = max(VPP_POWER_STEP, min(100, int(max_pct)))
    return int(min(pct, cap))


def watchdog_minutes(minutes: int) -> int:
    """Clamp and round the watchdog to the register's range/step."""
    m = int(round(max(VPP_TIME_STEP, min(1440, int(minutes))) / VPP_TIME_STEP) * VPP_TIME_STEP)
    return max(VPP_TIME_STEP, m)


def export_limit_pct(watts: int, rated_kw: float) -> int:
    """Map Solar AI's semantic export limits to Growatt's % register.

    10000 W ("unlimited") → 100 %; 25 W ("blocked") and 0 W → 0 %.
    Anything else is scaled to the rated power.
    """
    if watts >= 10000:
        return 100
    if watts <= 25 or rated_kw <= 0:
        return 0
    return int(max(0, min(100, round(watts / (rated_kw * 1000) * 100))))


class GrowattVppProfile(PluginProfile):
    plugin = "growatt"
    label = "Growatt (VPP remote control)"
    control_supported = True
    requires_verification = True

    sensor_keys = {
        CONF_BATTERY_SOC_ENTITY: ("battery_soc",),
        CONF_CELL_TEMP_ENTITY: ("battery_lowest_temperature", "battery_temperature",
                                "bms_1_temp_a"),
        CONF_BATTERY_CHARGE_ENTITY: ("battery_charge_power",),
        CONF_BATTERY_DISCHARGE_ENTITY: ("battery_discharge_power",),
        CONF_BATTERY_CHARGE_TOTAL_ENTITY: ("total_battery_input_energy",),
        CONF_BATTERY_DISCHARGE_TOTAL_ENTITY: ("total_battery_output_energy",),
        CONF_FOXESS_GRID_IMPORT_ENTITY: ("total_forward_power",),
        CONF_FOXESS_GRID_EXPORT_ENTITY: ("total_reverse_power",),
        CONF_FOXESS_PV_POWER_ENTITY: ("pv_power_total",),
        CONF_FOXESS_LOAD_POWER_ENTITY: ("total_load_power", "house_load"),
    }

    dashboard_keys = {
        "sensor.foxessmodbus_solar_energy_today": ("today_s_solar_energy",),
    }

    # GEN4/TL-XH: "EMS Discharging Stop SOC (on grid)" (reg 3067, newer
    # firmware) and "EMS Discharging Rate" (reg 3036). Both are disabled by
    # default in solax_modbus; the capability is on only if enabled.
    min_soc_key = "ems_discharging_stop_soc_on_grid"
    discharge_lock_key = "ems_discharging_rate"
    discharge_lock_restore_default = 100.0

    def keys_used(self) -> set[str]:
        return {
            KEY_VPP_STATUS, KEY_VPP_REMOTE, KEY_VPP_TIME, KEY_VPP_POWER,
            KEY_VPP_AC_CHARGE, KEY_LIMIT_GRID_EXPORT, KEY_GRID_EXPORT_LIMIT,
            self.min_soc_key, self.discharge_lock_key,
        }

    def verify_keys(self) -> set[str]:
        # vpp_time is a countdown on some firmware — never verified.
        return {
            KEY_VPP_REMOTE, KEY_VPP_POWER, KEY_VPP_AC_CHARGE,
            KEY_GRID_EXPORT_LIMIT, self.min_soc_key, self.discharge_lock_key,
        }

    def competing_controls(self, ctx: ProfileContext) -> list[str]:
        """Enabled GEN4 time-of-use slots in Battery First / Grid First mode
        change the battery priority underneath VPP control."""
        found = []
        for n in range(1, 10):
            if ctx.state(f"time_{n}_enabled") != ENABLED:
                continue
            mode = ctx.state(f"time_{n}_mode")
            if mode in ("Battery First", "Grid First"):
                found.append(f"time slot {n} ({mode})")
        return found

    # ---- modes -------------------------------------------------------

    def _vpp_enable_prefix(self, ctx: ProfileContext) -> list[Write]:
        if ctx.state(KEY_VPP_STATUS) == ENABLED:
            return []
        return [Write(PLATFORM_SELECT, KEY_VPP_STATUS, ENABLED)]

    def _plan_forced(self, signed_pct: int, ac_charge: bool,
                     ctx: ProfileContext) -> list[Write]:
        # Power and timeout are written BEFORE remote control is enabled, so
        # the inverter never acts on a stale power value (e.g. the previous
        # discharge setpoint) for a poll cycle.
        return [
            *self._vpp_enable_prefix(ctx),
            Write(PLATFORM_SELECT, KEY_VPP_AC_CHARGE, ENABLED if ac_charge else DISABLED),
            Write(PLATFORM_NUMBER, KEY_VPP_POWER, signed_pct),
            Write(PLATFORM_NUMBER, KEY_VPP_TIME, watchdog_minutes(ctx.watchdog_min)),
            Write(PLATFORM_SELECT, KEY_VPP_REMOTE, ENABLED),
        ]

    def plan_self_use(self, ctx: ProfileContext) -> list[Write]:
        return [Write(PLATFORM_SELECT, KEY_VPP_REMOTE, DISABLED)]

    def plan_force_charge(self, kw: float | None, ctx: ProfileContext) -> list[Write]:
        # kw None → mode requested without a power value; 0 % holds the
        # battery until a power value follows.
        pct = kw_to_vpp_pct(kw, ctx.rated_kw, ctx.max_pct) if kw else 0
        return self._plan_forced(ctx.sign * pct, True, ctx)

    def plan_force_discharge(self, kw: float, ctx: ProfileContext) -> list[Write]:
        pct = (kw_to_vpp_pct(kw, ctx.rated_kw, ctx.max_pct) if kw > 0
               else max(VPP_POWER_STEP, min(100, int(ctx.max_pct))))
        return self._plan_forced(-ctx.sign * pct, False, ctx)

    def plan_set_charge_power(self, kw: float, ctx: ProfileContext) -> list[Write]:
        pct = kw_to_vpp_pct(kw, ctx.rated_kw, ctx.max_pct)
        return [Write(PLATFORM_NUMBER, KEY_VPP_POWER, ctx.sign * pct)]

    def keepalive_interval_s(self, ctx: ProfileContext) -> float | None:
        # Refresh at a third of the watchdog so two missed refreshes still
        # leave the forced mode in place; never more often than once a minute.
        return max(60.0, watchdog_minutes(ctx.watchdog_min) * 60 / 3)

    def plan_keepalive(self, intent: str, last_power_pct: int | None,
                       ctx: ProfileContext) -> list[Write]:
        if intent not in (INTENT_FORCE_CHARGE, INTENT_FORCE_DISCHARGE) or last_power_pct is None:
            return []
        # Re-send the complete command. Whether a vpp_time write restarts the
        # countdown is not documented; re-sending power + enable as well makes
        # the keep-alive correct either way (and recovers from a timeout).
        return self._plan_forced(
            last_power_pct, intent == INTENT_FORCE_CHARGE, ctx)

    def last_power_pct(self, writes: list[Write]) -> int | None:
        for w in writes:
            if w.key == KEY_VPP_POWER:
                return int(w.value)
        return None

    def is_force_charging(self, ctx: ProfileContext) -> bool:
        if ctx.state(KEY_VPP_REMOTE) != ENABLED:
            return False
        try:
            return float(ctx.state(KEY_VPP_POWER)) * ctx.sign > 0
        except (TypeError, ValueError):
            return False

    # ---- export limit ------------------------------------------------

    def export_limit_supported(self, ctx: ProfileContext) -> bool:
        # Only when an export-limit meter is configured on the inverter; the
        # meter selection itself is installer territory and never changed.
        mode = ctx.state(KEY_LIMIT_GRID_EXPORT)
        return mode not in (None, DISABLED, "unknown", "unavailable")

    def plan_export_limit(self, watts: int, ctx: ProfileContext) -> list[Write]:
        return [Write(PLATFORM_NUMBER, KEY_GRID_EXPORT_LIMIT,
                      export_limit_pct(watts, ctx.rated_kw))]
