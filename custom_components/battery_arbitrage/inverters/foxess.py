"""FoxESS inverter backend (foxess_modbus integration).

The bodies below are the pre-abstraction coordinator helpers moved here
unchanged (work-mode select, force charge/discharge power numbers in kW,
export-limit register 46616, PV-limited flag register 49251), so FoxESS
installs behave exactly as before.
"""
from __future__ import annotations

import logging

from ..const import (
    CONF_FOXESS_FORCE_CHARGE_ENTITY,
    CONF_FOXESS_FORCE_DISCHARGE_ENTITY,
    CONF_FOXESS_INVERTER_ID,
    CONF_FOXESS_MAX_DISCHARGE_ENTITY,
    CONF_FOXESS_MIN_SOC_ENTITY,
    CONF_FOXESS_WORK_MODE_ENTITY,
    FOXESS_EXPORT_LIMIT_REGISTER,
    FOXESS_FORCE_CHARGE_ENTITY,
    FOXESS_FORCE_DISCHARGE_ENTITY,
    FOXESS_MAX_DISCHARGE_ENTITY,
    FOXESS_MIN_SOC_ON_GRID_ENTITY,
    FOXESS_PV_POWER_LIMITED_FLAG_REGISTER,
    FOXESS_WORK_MODE_ENTITY,
    INVERTER_BACKEND_FOXESS,
    WORK_MODE_FORCE_CHARGE,
    WORK_MODE_FORCE_DISCHARGE,
    WORK_MODE_SELF_USE,
)
from .base import InverterBackend, InverterCapabilities

_LOGGER = logging.getLogger(__name__)


class FoxessBackend(InverterBackend):
    """Controls a FoxESS inverter through the foxess_modbus integration."""

    backend_id = INVERTER_BACKEND_FOXESS
    discharge_lock_restore_default = 50.0

    @property
    def title(self) -> str:
        return "FoxESS Modbus"

    @property
    def _inverter_id(self) -> str:
        return self.config.get(CONF_FOXESS_INVERTER_ID, "")

    @property
    def _work_mode_entity(self) -> str:
        return self.config.get(CONF_FOXESS_WORK_MODE_ENTITY, FOXESS_WORK_MODE_ENTITY)

    def capabilities(self) -> InverterCapabilities:
        return InverterCapabilities(
            export_limit=True,
            pv_limited_flag=True,
            min_soc_backstop=True,
            discharge_lock=True,
        )

    # ---- operating modes ---------------------------------------------

    async def _set_work_mode(self, mode: str) -> None:
        entity = self._work_mode_entity
        try:
            await self.actuator.async_write(
                "select", "select_option", {"entity_id": entity, "option": mode})
            _LOGGER.debug("Battery Arbitrage: set work mode → %s", mode)
        except Exception as err:
            _LOGGER.error("Failed to set FoxESS work mode to %s: %s", mode, err)

    async def async_self_use(self) -> None:
        await self._set_work_mode(WORK_MODE_SELF_USE)

    async def async_force_charge(self, kw: float | None) -> None:
        await self._set_work_mode(WORK_MODE_FORCE_CHARGE)
        if kw is not None:
            await self.async_set_charge_power(kw)

    async def async_force_discharge(self, kw: float) -> None:
        await self._set_work_mode(WORK_MODE_FORCE_DISCHARGE)
        await self._set_discharge_power(kw)

    async def async_set_charge_power(self, kw: float) -> None:
        entity = self.config.get(CONF_FOXESS_FORCE_CHARGE_ENTITY, FOXESS_FORCE_CHARGE_ENTITY)
        # v0.47.6 — the FoxESS force-charge-power entity is in kW (not W). The
        # previous `int(rate_kw * 1000)` wrote watts into a 0–10 kW field, so the
        # set silently failed (out of range) and the power stayed at its max,
        # ignoring the grid-headroom cap. Write kW, clamped to the entity range.
        st = self.hass.states.get(entity)
        ent_max = float(st.attributes.get("max", 10.0)) if st else 10.0
        value = round(max(0.0, min(kw, ent_max)), 3)
        try:
            await self.actuator.async_write(
                "number", "set_value", {"entity_id": entity, "value": value})
            _LOGGER.debug("Battery Arbitrage: force charge power → %.3f kW", value)
        except Exception as err:
            _LOGGER.error("Failed to set force charge power: %s", err)

    async def _set_discharge_power(self, max_kw: float) -> None:
        """Set the Force Discharge power (kW). `max_kw <= 0` → full rate (the
        entity's max).

        v0.47.6 — the FoxESS force-discharge-power entity is in kW (not W); the
        previous `int(max_kw * 1000)` wrote watts into a 0–10 kW field and the
        set silently failed. Write kW, clamped to the entity range.
        """
        entity = self.config.get(CONF_FOXESS_FORCE_DISCHARGE_ENTITY, FOXESS_FORCE_DISCHARGE_ENTITY)
        st = self.hass.states.get(entity)
        ent_max = float(st.attributes.get("max", 10.0)) if st else 10.0
        value = round(ent_max if max_kw <= 0 else max(0.0, min(float(max_kw), ent_max)), 3)
        try:
            await self.actuator.async_write(
                "number", "set_value", {"entity_id": entity, "value": value})
            _LOGGER.debug("Battery Arbitrage: force discharge power → %.3f kW", value)
        except Exception as err:
            _LOGGER.error("Failed to set force discharge power: %s", err)

    def is_force_charging(self) -> bool:
        wm = self.hass.states.get(self._work_mode_entity)
        return wm is not None and wm.state == WORK_MODE_FORCE_CHARGE

    def current_work_mode(self) -> str:
        wm = self.hass.states.get(self._work_mode_entity)
        return wm.state if wm else WORK_MODE_SELF_USE

    # ---- hardware helpers --------------------------------------------

    async def async_set_export_limit(self, watts: int) -> None:
        """Write the FoxESS export limit register (46616)."""
        try:
            high = watts // 65536
            low = watts % 65536
            await self.actuator.async_write(
                "foxess_modbus", "write_registers",
                {
                    "inverter": self._inverter_id,
                    "start_address": FOXESS_EXPORT_LIMIT_REGISTER,
                    "values": f"{high}, {low}",
                },
            )
            _LOGGER.debug("Battery Arbitrage: export limit → %dW", watts)
        except Exception as err:
            _LOGGER.error("Failed to set export limit: %s", err)

    async def async_read_pv_limited(self) -> bool | None:
        """Read the FoxESS "PV Power Limited" holding register (49251).

        Returns True when the inverter reports it is actively curtailing PV
        (MPPT throttled), False when the inverter is delivering all
        available PV, or None if the read fails. Used by the EV controller
        as the curtailment trigger (v0.36.2 — replaces the v0.30.1
        forecast-substitution heuristic).
        """
        inverter_id = self._inverter_id
        if not inverter_id:
            return None
        try:
            resp = await self.hass.services.async_call(
                "foxess_modbus", "read_registers",
                {
                    "inverter": inverter_id,
                    "start_address": FOXESS_PV_POWER_LIMITED_FLAG_REGISTER,
                    "count": 1,
                    "type": "holding",
                },
                blocking=True,
                return_response=True,
            )
            values = ((resp or {}).get("values")
                      or (resp or {}).get("response", {}).get("values")
                      or {})
            raw = values.get(FOXESS_PV_POWER_LIMITED_FLAG_REGISTER)
            if raw is None:
                # Some service responses key by stringified address
                raw = values.get(str(FOXESS_PV_POWER_LIMITED_FLAG_REGISTER))
            return bool(int(raw)) if raw is not None else None
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Read PV-limited flag (reg 49251) failed: %s", err)
            return None

    def min_soc_entity(self) -> str | None:
        return self.config.get(CONF_FOXESS_MIN_SOC_ENTITY, FOXESS_MIN_SOC_ON_GRID_ENTITY)

    def discharge_lock_entity(self) -> str | None:
        return self.config.get(CONF_FOXESS_MAX_DISCHARGE_ENTITY, FOXESS_MAX_DISCHARGE_ENTITY)
