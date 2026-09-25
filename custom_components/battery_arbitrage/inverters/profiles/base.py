"""Plugin profiles for the solax_modbus backend.

solax_modbus supports many inverter brands through per-brand plugins, each
with its own control registers. A `PluginProfile` maps Solar AI's intents
(force charge, force discharge, self use, export limit, …) to a list of
entity writes for one plugin. Profiles are pure command planners: they never
touch Home Assistant, which keeps them trivially unit-testable. The
`SolaxModbusBackend` resolves the entity keys and executes the writes.

Entity keys are solax_modbus entity-description keys; the backend resolves
them to entity_ids through the entity registry (unique_id = f"{hub}_{key}").
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

# Platforms a Write can target, and the HA service used for each.
PLATFORM_SELECT = "select"
PLATFORM_NUMBER = "number"
PLATFORM_BUTTON = "button"

# Intent names (also used as the backend's remembered mode).
INTENT_SELF_USE = "self_use"
INTENT_FORCE_CHARGE = "force_charge"
INTENT_FORCE_DISCHARGE = "force_discharge"


@dataclass(frozen=True)
class Write:
    """One entity write: select option, number value, or button press."""

    platform: str
    key: str
    value: Any = None


@dataclass
class ProfileContext:
    """Everything a profile needs to plan writes."""

    rated_kw: float                     # inverter rated AC power
    state: Callable[[str], str | None]  # entity key → current state string
    sign: int = 1                       # verified control direction (+1 / -1)
    max_pct: int = 100                  # safety cap on the power command (%)
    watchdog_min: int = 15              # hardware fallback timeout (minutes)


class PluginProfile:
    """Base profile: sensing only, no control."""

    plugin: str = ""
    label: str = ""
    control_supported: bool = False
    # Whether the direction of the power command must be verified on the
    # hardware (self-test) before Solar AI may actuate.
    requires_verification: bool = True

    # Solar AI config key → candidate solax_modbus entity keys, in order of
    # preference. Used by discovery to prefill the config flow.
    sensor_keys: dict[str, tuple[str, ...]] = {}

    # Optional capability entity keys (None → capability absent).
    min_soc_key: str | None = None
    discharge_lock_key: str | None = None
    discharge_lock_restore_default: float = 100.0

    def verify_keys(self) -> set[str]:
        """Keys whose written value should stay put (write verification).
        Exclude registers the inverter changes by itself (e.g. countdowns)."""
        return set()

    def competing_controls(self, ctx: ProfileContext) -> list[str]:
        """Human-readable list of inverter-side schedules that would fight
        Solar AI (e.g. enabled time-of-use slots). Empty when none."""
        return []

    def keys_used(self) -> set[str]:
        """Every control entity key this profile may write (for diagnostics
        and for checking which entities exist / are enabled)."""
        return set()

    def plan_self_use(self, ctx: ProfileContext) -> list[Write]:
        return []

    def plan_force_charge(self, kw: float | None, ctx: ProfileContext) -> list[Write]:
        return []

    def plan_force_discharge(self, kw: float, ctx: ProfileContext) -> list[Write]:
        return []

    def plan_set_charge_power(self, kw: float, ctx: ProfileContext) -> list[Write]:
        return []

    def plan_keepalive(self, intent: str, last_power_pct: int | None,
                       ctx: ProfileContext) -> list[Write]:
        """Writes to repeat periodically while a forced mode is active."""
        return []

    def keepalive_interval_s(self, ctx: ProfileContext) -> float | None:
        """How often plan_keepalive should run, or None for never."""
        return None

    def export_limit_supported(self, ctx: ProfileContext) -> bool:
        return False

    def plan_export_limit(self, watts: int, ctx: ProfileContext) -> list[Write]:
        return []

    def is_force_charging(self, ctx: ProfileContext) -> bool:
        return False

    def last_power_pct(self, writes: list[Write]) -> int | None:
        """The power command contained in a write plan, if any."""
        return None


def utc_seconds_since(ts: datetime | None, now: datetime) -> float:
    if ts is None:
        return float("inf")
    return (now - ts).total_seconds()
