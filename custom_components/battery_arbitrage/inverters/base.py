"""Inverter backend interface.

Solar AI's decision layer works with three operating modes (self use, grid
charging, exporting) plus a few hardware helpers (export limit, min-SoC
backstop, battery discharge lock). An `InverterBackend` translates those
intents into writes for one inverter integration.

All writes go through the shared `Actuator` (dry run / blocked / context
tracking); backends never call `hass.services` directly for writes.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from ..actuation import Actuator


@dataclass
class InverterCapabilities:
    """Optional features. The coordinator skips a feature the backend lacks."""

    export_limit: bool = False       # can cap / block grid export
    pv_limited_flag: bool = False    # can report MPPT curtailment
    min_soc_backstop: bool = False   # has a writable on-grid min-SoC
    discharge_lock: bool = False     # can stop battery discharge (EV lock)


class InverterBackend(ABC):
    """Base class for inverter backends."""

    backend_id: str = ""
    # When True, control writes stay blocked until the commissioning
    # self-test has verified the control direction on this inverter.
    requires_verification: bool = False
    # Fallback restore value for the discharge-lock entity when the value
    # before locking is unknown.
    discharge_lock_restore_default: float = 50.0

    def __init__(
        self,
        hass: HomeAssistant,
        config: dict[str, Any],
        actuator: Actuator,
        store: Callable[[], dict[str, Any]],
        save: Callable[[], None] = lambda: None,
    ) -> None:
        self.hass = hass
        self.config = config
        self.actuator = actuator
        self._store = store
        self._save = save

    # ---- description -------------------------------------------------

    @property
    def title(self) -> str:
        return self.backend_id

    @abstractmethod
    def capabilities(self) -> InverterCapabilities:
        """Capabilities right now (may change as entities come and go)."""

    def blocked_reason(self) -> str | None:
        """Why control writes are currently refused, or None."""
        return None

    def diagnostics(self) -> dict[str, Any]:
        caps = self.capabilities()
        return {
            "backend": self.backend_id,
            "title": self.title,
            "blocked": self.blocked_reason(),
            "capabilities": {
                "export_limit": caps.export_limit,
                "pv_limited_flag": caps.pv_limited_flag,
                "min_soc_backstop": caps.min_soc_backstop,
                "discharge_lock": caps.discharge_lock,
            },
        }

    # ---- lifecycle ---------------------------------------------------

    async def async_setup(self) -> None:
        """One-off checks after HA start (repair issues etc.)."""

    async def async_tick(self, now: datetime) -> None:
        """Called every coordinator cycle (keep-alives, write verification)."""

    # ---- operating modes ---------------------------------------------

    @abstractmethod
    async def async_self_use(self) -> None:
        """Return the battery to the inverter's own self-consumption logic."""

    @abstractmethod
    async def async_force_charge(self, kw: float | None) -> None:
        """Charge the battery from the grid. `kw` None → mode only, keep power."""

    @abstractmethod
    async def async_force_discharge(self, kw: float) -> None:
        """Discharge the battery to the grid. `kw` <= 0 → full rate."""

    @abstractmethod
    async def async_set_charge_power(self, kw: float) -> None:
        """Re-cap the grid-charge power while force charging."""

    @abstractmethod
    def is_force_charging(self) -> bool:
        """True when the inverter reports it is in grid-charge mode."""

    # ---- optional hardware helpers -----------------------------------

    async def async_set_number(self, entity_id: str, value: float) -> None:
        """Write a number entity owned by the inverter (min-SoC backstop,
        discharge lock). Raises on failure, like a direct service call."""
        await self.actuator.async_write(
            "number", "set_value", {"entity_id": entity_id, "value": value},
            blocked_reason=self.blocked_reason())

    async def async_set_export_limit(self, watts: int) -> None:
        """Cap grid export. Semantic values: 0 (charging), 25 (blocked),
        10000 (unlimited). Only called when capabilities().export_limit."""

    async def async_read_pv_limited(self) -> bool | None:
        """MPPT-curtailment flag, or None when unknown."""
        return None

    def min_soc_entity(self) -> str | None:
        """Writable on-grid min-SoC number entity (%), or None."""
        return None

    def discharge_lock_entity(self) -> str | None:
        """Number entity that stops battery discharge when set to 0, or None."""
        return None

    # ---- commissioning -----------------------------------------------

    async def async_self_test(
        self,
        read_net_battery_kw: Callable[[], float | None],
        read_soc: Callable[[], float | None],
        sleep: Callable[[float], Awaitable[None]],
    ) -> dict[str, Any]:
        """Verify the control direction on real hardware. Backends that need
        no verification report success without touching the inverter."""
        return {"ok": True, "detail": "No verification needed for this backend."}
