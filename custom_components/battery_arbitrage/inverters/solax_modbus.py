"""solax_modbus inverter backend.

Controls an inverter connected through the solax_modbus integration (which
covers SolaX, Growatt, Solis, Sofar, … via per-brand plugins). The brand
specific part lives in a `PluginProfile`; this class resolves entity keys to
entity_ids, executes the planned writes through the Actuator, keeps timed
remote-control modes alive, verifies writes, and runs the commissioning
self-test.

Entities are resolved through the entity registry by unique_id
(`f"{hub_name}_{key}"`, as solax_modbus creates them), never by building
entity_id strings — entity_ids are user-renamable and hub-name dependent.
Only standard HA services (select/number/button) are used; solax_modbus
internals are never touched.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from ..actuation import WriteVerifier
from ..const import (
    COMMISSIONING_CYCLES,
    COMMISSIONING_MAX_PCT,
    CONF_INVERTER_RATED_KW,
    CONF_SOLAX_HUB_NAME,
    CONF_VPP_WATCHDOG_MIN,
    DEFAULT_INVERTER_RATED_KW,
    DEFAULT_VPP_WATCHDOG_MIN,
    DOMAIN,
    INVERTER_BACKEND_SOLAX,
)
from .base import InverterBackend, InverterCapabilities
from .profiles import get_profile
from .profiles.base import (
    INTENT_FORCE_CHARGE,
    INTENT_FORCE_DISCHARGE,
    INTENT_SELF_USE,
    PLATFORM_BUTTON,
    PLATFORM_NUMBER,
    PLATFORM_SELECT,
    ProfileContext,
    Write,
    utc_seconds_since,
)

_LOGGER = logging.getLogger(__name__)

SOLAX_DOMAIN = "solax_modbus"
EVCC_PROXY_PORT = 1502

ISSUE_EVCC_PRESENT = "evcc_present"
ISSUE_COMPETING_SCHEDULE = "inverter_competing_schedule"
ISSUE_WRITE_MISMATCH = "inverter_write_mismatch"
ISSUE_NOT_VERIFIED = "inverter_not_verified"

STORE_VERIFICATION = "inverter_verification"
STORE_OK_CYCLES = "inverter_ok_cycles"

_SERVICE_FOR_PLATFORM = {
    PLATFORM_SELECT: ("select_option", "option"),
    PLATFORM_NUMBER: ("set_value", "value"),
    PLATFORM_BUTTON: ("press", None),
}
_STATE_PLATFORMS = ("select", "number", "sensor", "time", "switch")
_UNAVAILABLE = (None, "unknown", "unavailable")


def solax_hubs(hass) -> list[dict[str, Any]]:
    """All loaded solax_modbus hubs: name, title, plugin, host, port."""
    hubs = []
    for entry in hass.config_entries.async_entries(SOLAX_DOMAIN):
        if entry.disabled_by:
            continue
        opts = {**entry.data, **entry.options}
        name = opts.get("name")
        if not name:
            continue
        hubs.append({
            "name": name,
            "title": entry.title,
            "plugin": (opts.get("plugin") or "").lower(),
            "host": opts.get("host"),
            "port": opts.get("port"),
            "inverter_power_kw": opts.get("inverter_power_kw"),
        })
    return hubs


def resolve_key(hass, hub_name: str, key: str,
                platforms: tuple[str, ...] = _STATE_PLATFORMS) -> str | None:
    """entity_id for a solax_modbus entity key on a hub, or None."""
    reg = er.async_get(hass)
    uid = f"{hub_name}_{key}"
    for platform in platforms:
        entity_id = reg.async_get_entity_id(platform, SOLAX_DOMAIN, uid)
        if entity_id:
            return entity_id
    return None


def discover(hass, hub_name: str) -> dict[str, str]:
    """Prefill Solar AI's sensor config keys for a solax_modbus hub."""
    hub = next((h for h in solax_hubs(hass) if h["name"] == hub_name), None)
    profile = get_profile(hub["plugin"]) if hub else None
    if profile is None:
        return {}
    found: dict[str, str] = {}
    for conf_key, candidates in profile.sensor_keys.items():
        for key in candidates:
            entity_id = resolve_key(hass, hub_name, key, ("sensor",))
            if entity_id:
                found[conf_key] = entity_id
                break
    return found


class SolaxModbusBackend(InverterBackend):
    """Controls a battery through the solax_modbus integration."""

    backend_id = INVERTER_BACKEND_SOLAX

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.hub_name: str = self.config.get(CONF_SOLAX_HUB_NAME, "")
        hub = next((h for h in solax_hubs(self.hass) if h["name"] == self.hub_name), None)
        self._hub = hub or {"name": self.hub_name, "plugin": "", "title": self.hub_name}
        self.profile = get_profile(self._hub.get("plugin"))
        self.requires_verification = bool(self.profile and self.profile.requires_verification)
        if self.profile:
            self.discharge_lock_restore_default = self.profile.discharge_lock_restore_default
        self._entity_cache: dict[str, str | None] = {}
        self._intent: str = INTENT_SELF_USE
        self._last_power_pct: int | None = None
        self._last_keepalive: datetime | None = None
        self._cycle_check_after: datetime | None = None
        self.verifier = WriteVerifier()
        self._missing_logged: set[str] = set()

    # ---- description -------------------------------------------------

    @property
    def title(self) -> str:
        label = self.profile.label if self.profile else f"unsupported plugin '{self._hub.get('plugin')}'"
        return f"solax_modbus {self._hub.get('title', self.hub_name)} — {label}"

    @property
    def rated_kw(self) -> float:
        try:
            return float(self.config.get(CONF_INVERTER_RATED_KW) or DEFAULT_INVERTER_RATED_KW)
        except (TypeError, ValueError):
            return DEFAULT_INVERTER_RATED_KW

    def _verification(self) -> dict[str, Any] | None:
        rec = self._store().get(STORE_VERIFICATION)
        if (isinstance(rec, dict)
                and rec.get("backend") == self.backend_id
                and rec.get("hub") == self.hub_name
                and rec.get("plugin") == self._hub.get("plugin")):
            return rec
        return None

    def _ctx(self) -> ProfileContext:
        rec = self._verification()
        ok_cycles = int(self._store().get(STORE_OK_CYCLES, 0) or 0)
        return ProfileContext(
            rated_kw=self.rated_kw,
            state=self._state_by_key,
            sign=int(rec.get("sign", 1)) if rec else 1,
            max_pct=100 if ok_cycles >= COMMISSIONING_CYCLES else COMMISSIONING_MAX_PCT,
            watchdog_min=int(self.config.get(CONF_VPP_WATCHDOG_MIN, DEFAULT_VPP_WATCHDOG_MIN)),
        )

    def blocked_reason(self) -> str | None:
        if self.profile is None:
            return f"no profile for solax_modbus plugin '{self._hub.get('plugin')}'"
        if not self.profile.control_supported:
            return f"control not implemented for plugin '{self.profile.plugin}'"
        if self.requires_verification and self._verification() is None:
            return "not verified — run the battery_arbitrage.inverter_self_test service"
        return None

    # ---- entity helpers ----------------------------------------------

    def _entity(self, key: str | None) -> str | None:
        if not key:
            return None
        if self._entity_cache.get(key) is None:
            self._entity_cache[key] = resolve_key(self.hass, self.hub_name, key)
        return self._entity_cache[key]

    def _state_by_key(self, key: str) -> str | None:
        entity_id = self._entity(key)
        if not entity_id:
            return None
        st = self.hass.states.get(entity_id)
        return st.state if st else None

    def _available(self, key: str | None) -> bool:
        entity_id = self._entity(key)
        if not entity_id:
            return False
        st = self.hass.states.get(entity_id)
        return st is not None and st.state not in _UNAVAILABLE

    def capabilities(self) -> InverterCapabilities:
        if self.profile is None or not self.profile.control_supported:
            return InverterCapabilities()
        return InverterCapabilities(
            export_limit=self.profile.export_limit_supported(self._ctx()),
            pv_limited_flag=False,
            min_soc_backstop=self._available(self.profile.min_soc_key),
            discharge_lock=self._available(self.profile.discharge_lock_key),
        )

    def diagnostics(self) -> dict[str, Any]:
        out = super().diagnostics()
        rec = self._verification()
        out.update({
            "hub": self.hub_name,
            "plugin": self._hub.get("plugin"),
            "rated_kw": self.rated_kw,
            "intent": self._intent,
            "last_power_pct": self._last_power_pct,
            "verified_sign": rec.get("sign") if rec else None,
            "verified_at": rec.get("time") if rec else None,
            "confirmed_cycles": int(self._store().get(STORE_OK_CYCLES, 0) or 0),
            "control_entities": {
                key: self._entity(key) for key in sorted(
                    k for k in (self.profile.keys_used() if self.profile else set()) if k)
            },
        })
        return out

    # ---- write execution ---------------------------------------------

    async def _execute(self, writes: list[Write], *, bypass_block: bool = False) -> bool:
        """Run a write plan in order. Stops at the first failure, since later
        writes (e.g. enabling remote control) assume earlier ones landed.
        Returns True when every write was sent or simulated."""
        blocked = None if bypass_block else self.blocked_reason()
        now = datetime.now(timezone.utc)
        verify = self.profile.verify_keys() if self.profile else set()
        for w in writes:
            entity_id = self._entity(w.key)
            if not entity_id:
                if w.key not in self._missing_logged:
                    self._missing_logged.add(w.key)
                    _LOGGER.error(
                        "solax_modbus hub '%s': entity for key '%s' not found — "
                        "is it enabled in the solax_modbus integration?",
                        self.hub_name, w.key)
                return False
            service, field = _SERVICE_FOR_PLATFORM[w.platform]
            data: dict[str, Any] = {"entity_id": entity_id}
            if field:
                data[field] = w.value
            try:
                sent = await self.actuator.async_write(
                    w.platform, service, data, blocked_reason=blocked)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("solax_modbus write %s.%s %s failed: %s",
                              w.platform, service, data, err)
                return False
            if sent and w.key in verify:
                self.verifier.expect(entity_id, w.value, now)
        return True

    # ---- operating modes ---------------------------------------------

    async def async_self_use(self) -> None:
        if not self.profile:
            return
        await self._execute(self.profile.plan_self_use(self._ctx()))
        self._intent = INTENT_SELF_USE
        self._last_power_pct = None
        self._cycle_check_after = None

    async def _enter_forced(self, intent: str, writes: list[Write]) -> None:
        ok = await self._execute(writes)
        self._intent = intent
        self._last_power_pct = self.profile.last_power_pct(writes)
        now = datetime.now(timezone.utc)
        self._last_keepalive = now
        if ok and not self.blocked_reason() and not self.actuator.dry_run:
            self._cycle_check_after = now

    async def async_force_charge(self, kw: float | None) -> None:
        if not self.profile:
            return
        await self._enter_forced(
            INTENT_FORCE_CHARGE, self.profile.plan_force_charge(kw, self._ctx()))

    async def async_force_discharge(self, kw: float) -> None:
        if not self.profile:
            return
        await self._enter_forced(
            INTENT_FORCE_DISCHARGE, self.profile.plan_force_discharge(kw, self._ctx()))

    async def async_set_charge_power(self, kw: float) -> None:
        if not self.profile or self._intent != INTENT_FORCE_CHARGE:
            return
        writes = self.profile.plan_set_charge_power(kw, self._ctx())
        if await self._execute(writes):
            pct = self.profile.last_power_pct(writes)
            if pct is not None:
                self._last_power_pct = pct

    def is_force_charging(self) -> bool:
        if not self.profile:
            return False
        if self.actuator.dry_run or self.blocked_reason():
            # Nothing was sent, so the hardware can't confirm; report the
            # simulated mode so the control logic runs as it would live.
            return self._intent == INTENT_FORCE_CHARGE
        return self.profile.is_force_charging(self._ctx())

    # ---- hardware helpers --------------------------------------------

    async def async_set_export_limit(self, watts: int) -> None:
        if not self.profile:
            return
        await self._execute(self.profile.plan_export_limit(watts, self._ctx()))

    def min_soc_entity(self) -> str | None:
        return self._entity(self.profile.min_soc_key) if self.profile else None

    def discharge_lock_entity(self) -> str | None:
        return self._entity(self.profile.discharge_lock_key) if self.profile else None

    async def async_set_number(self, entity_id: str, value: float) -> None:
        sent = await self.actuator.async_write(
            "number", "set_value", {"entity_id": entity_id, "value": value},
            blocked_reason=self.blocked_reason())
        if sent:
            self.verifier.expect(entity_id, value, datetime.now(timezone.utc))

    # ---- lifecycle ---------------------------------------------------

    async def async_setup(self) -> None:
        """Startup checks → repair issues (never blocks setup)."""
        evcc_reasons = []
        if self.hass.config_entries.async_entries("evcc_intg"):
            evcc_reasons.append("the evcc integration is installed")
        try:
            if int(self._hub.get("port") or 0) == EVCC_PROXY_PORT:
                evcc_reasons.append(
                    f"hub '{self.hub_name}' connects on port {EVCC_PROXY_PORT} (evcc's Modbus proxy)")
        except (TypeError, ValueError):
            pass
        self._set_issue(ISSUE_EVCC_PRESENT, bool(evcc_reasons),
                        {"reasons": "; ".join(evcc_reasons)})
        self._check_competing()
        self._set_issue(ISSUE_NOT_VERIFIED,
                        bool(self.profile and self.profile.control_supported
                             and self.requires_verification and self._verification() is None),
                        {"hub": self.hub_name})

    def _check_competing(self) -> None:
        if not self.profile:
            return
        found = self.profile.competing_controls(self._ctx())
        self._set_issue(ISSUE_COMPETING_SCHEDULE, bool(found),
                        {"hub": self.hub_name, "controls": ", ".join(found)})

    def _set_issue(self, issue_id: str, active: bool,
                   placeholders: dict[str, str] | None = None) -> None:
        try:
            if active:
                ir.async_create_issue(
                    self.hass, DOMAIN, issue_id, is_fixable=False,
                    severity=ir.IssueSeverity.WARNING, translation_key=issue_id,
                    translation_placeholders=placeholders or {})
            else:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Repair issue %s update failed: %s", issue_id, err)

    async def async_tick(self, now: datetime) -> None:
        if not self.profile or not self.profile.control_supported:
            return
        ctx = self._ctx()

        # 1. Keep a timed forced mode alive.
        interval = self.profile.keepalive_interval_s(ctx)
        if (interval is not None
                and self._intent in (INTENT_FORCE_CHARGE, INTENT_FORCE_DISCHARGE)
                and utc_seconds_since(self._last_keepalive, now) >= interval):
            self._last_keepalive = now
            await self._execute(
                self.profile.plan_keepalive(self._intent, self._last_power_pct, ctx))

        # 2. Verify our writes stuck; re-assert (rate-limited) and escalate.
        mismatches = self.verifier.evaluate(
            lambda eid: (st.state if (st := self.hass.states.get(eid)) else None), now)
        for m in mismatches:
            _LOGGER.warning(
                "Inverter entity %s is %r, expected %r (mismatch #%d)%s",
                m.entity_id, m.actual, m.expected, m.count,
                " — re-asserting" if m.reassert else "")
            if m.reassert:
                platform = m.entity_id.split(".", 1)[0]
                service, field = _SERVICE_FOR_PLATFORM.get(platform, (None, None))
                if service and field:
                    try:
                        await self.actuator.async_write(
                            platform, service,
                            {"entity_id": m.entity_id, field: m.expected},
                            blocked_reason=self.blocked_reason())
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.error("Re-assert of %s failed: %s", m.entity_id, err)
        persistent = [m for m in mismatches if m.persistent]
        if persistent or not mismatches:
            self._set_issue(ISSUE_WRITE_MISMATCH, bool(persistent), {
                "entities": ", ".join(
                    f"{m.entity_id} (is {m.actual}, expected {m.expected})" for m in persistent),
            })

        # 3. Commissioning: count forced cycles the inverter confirmed.
        if (self._cycle_check_after is not None
                and utc_seconds_since(self._cycle_check_after, now) >= self.verifier.grace_s):
            self._cycle_check_after = None
            if not mismatches:
                store = self._store()
                store[STORE_OK_CYCLES] = int(store.get(STORE_OK_CYCLES, 0) or 0) + 1
                self._save()

        # 4. Competing schedules can be switched on at any time.
        self._check_competing()

    # ---- commissioning self-test ---------------------------------------

    async def async_self_test(
        self,
        read_net_battery_kw: Callable[[], float | None],
        read_soc: Callable[[], float | None],
        sleep: Callable[[float], Awaitable[None]],
    ) -> dict[str, Any]:
        """Measure the battery's response to a small, short charge command and
        store the verified control direction.

        Commands 10 % of rated power "charge" (assuming sign +1) with a 5-min
        hardware watchdog, watches net battery power (charge − discharge) for
        up to 2 minutes, then always returns to self use. A clear rise means
        +1, a clear fall means the sign is inverted (−1); anything smaller is
        inconclusive and nothing is stored.
        """
        if self.profile is None or not self.profile.control_supported:
            return {"ok": False, "detail": self.blocked_reason()}
        if self.actuator.dry_run:
            return {"ok": False, "detail": "Dry run is on — turn it off to run the self-test."}
        soc = read_soc()
        if soc is None or not 15 <= soc <= 90:
            return {"ok": False,
                    "detail": f"Battery SoC must be between 15 and 90 % (now {soc})."}

        samples = []
        for _ in range(3):
            v = read_net_battery_kw()
            if v is not None:
                samples.append(v)
            await sleep(10)
        if not samples:
            return {"ok": False, "detail": "Battery power sensors are unavailable."}
        baseline = sum(samples) / len(samples)

        ctx = self._ctx()
        ctx.sign = 1
        ctx.watchdog_min = 5
        pct = 10
        expected_kw = self.rated_kw * pct / 100
        writes = self.profile.plan_force_charge(expected_kw, ctx)
        delta = 0.0
        try:
            if not await self._execute(writes, bypass_block=True):
                return {"ok": False, "detail": "Could not send the test command (see log)."}
            for _ in range(12):
                await sleep(10)
                v = read_net_battery_kw()
                if v is None:
                    continue
                delta = v - baseline
                if abs(delta) >= 0.5 * expected_kw:
                    break
        finally:
            await self._execute(self.profile.plan_self_use(ctx), bypass_block=True)
            self._intent = INTENT_SELF_USE
            self._last_power_pct = None

        if delta >= 0.5 * expected_kw:
            sign = 1
        elif delta <= -0.5 * expected_kw:
            sign = -1
        else:
            return {"ok": False,
                    "detail": (f"Inconclusive: battery power changed {delta:+.2f} kW for a "
                               f"{expected_kw:.2f} kW command. Nothing stored.")}
        rec = {
            "backend": self.backend_id,
            "hub": self.hub_name,
            "plugin": self._hub.get("plugin"),
            "sign": sign,
            "ratio": round(abs(delta) / expected_kw, 2),
            "time": datetime.now(timezone.utc).isoformat(),
        }
        store = self._store()
        store[STORE_VERIFICATION] = rec
        store[STORE_OK_CYCLES] = 0
        self._save()
        self._set_issue(ISSUE_NOT_VERIFIED, False)
        return {"ok": True, **rec,
                "detail": (f"Verified: charge command gave {delta:+.2f} kW "
                           f"(expected {expected_kw:.2f} kW); sign {sign:+d}.")}
