"""Inverter backend tests: FoxESS parity and solax_modbus/Growatt behaviour."""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from _bootstrap import ISSUES, REGISTRY, FakeEntry, FakeHass, make_actuator

from battery_arbitrage.const import (
    CONF_INVERTER_BACKEND,
    CONF_INVERTER_RATED_KW,
    CONF_SOLAX_HUB_NAME,
    INVERTER_BACKEND_SOLAX,
)
from battery_arbitrage.inverters.foxess import FoxessBackend
from battery_arbitrage.inverters.solax_modbus import (
    STORE_OK_CYCLES,
    STORE_VERIFICATION,
    SolaxModbusBackend,
    discover,
)

T0 = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


class FoxessParityTest(unittest.TestCase):
    """The FoxESS backend must issue exactly the calls the pre-v1.21 helpers did."""

    def setUp(self):
        self.hass = FakeHass()
        self.hass.services.reflect = False
        self.hass.states.set("number.foxessmodbus_force_charge_power", 0, max=10.0)
        self.hass.states.set("number.foxessmodbus_force_discharge_power", 0, max=8.0)
        self.b = FoxessBackend(self.hass, {"foxess_inverter_id": "FoxessModbus"},
                               make_actuator(self.hass), lambda: {})

    def test_force_charge(self):
        run(self.b.async_force_charge(3.25))
        self.assertEqual(self.hass.services.calls, [
            ("select", "select_option",
             {"entity_id": "select.foxessmodbus_work_mode", "option": "Force Charge"}),
            ("number", "set_value",
             {"entity_id": "number.foxessmodbus_force_charge_power", "value": 3.25}),
        ])

    def test_force_charge_mode_only_and_clamp(self):
        run(self.b.async_force_charge(None))
        self.assertEqual(len(self.hass.services.calls), 1)
        run(self.b.async_set_charge_power(42.0))
        self.assertEqual(self.hass.services.calls[-1][2]["value"], 10.0)

    def test_force_discharge_full_rate(self):
        run(self.b.async_force_discharge(0))
        self.assertEqual(self.hass.services.calls, [
            ("select", "select_option",
             {"entity_id": "select.foxessmodbus_work_mode", "option": "Force Discharge"}),
            ("number", "set_value",
             {"entity_id": "number.foxessmodbus_force_discharge_power", "value": 8.0}),
        ])

    def test_self_use(self):
        run(self.b.async_self_use())
        self.assertEqual(self.hass.services.calls[-1][2]["option"], "Self Use")

    def test_export_limit_register(self):
        run(self.b.async_set_export_limit(25))
        run(self.b.async_set_export_limit(70000))
        self.assertEqual(self.hass.services.calls, [
            ("foxess_modbus", "write_registers",
             {"inverter": "FoxessModbus", "start_address": 46616, "values": "0, 25"}),
            ("foxess_modbus", "write_registers",
             {"inverter": "FoxessModbus", "start_address": 46616, "values": "1, 4464"}),
        ])

    def test_force_charging_readback(self):
        self.hass.states.set("select.foxessmodbus_work_mode", "Force Charge")
        self.assertTrue(self.b.is_force_charging())
        self.hass.states.set("select.foxessmodbus_work_mode", "Self Use")
        self.assertFalse(self.b.is_force_charging())

    def test_defaults(self):
        self.assertEqual(self.b.min_soc_entity(), "number.foxessmodbus_min_soc_on_grid")
        self.assertEqual(self.b.discharge_lock_entity(), "number.foxessmodbus_max_discharge_current")
        self.assertIsNone(self.b.blocked_reason())
        self.assertTrue(self.b.capabilities().export_limit)


HUB = "evcc"
KEYS = {
    ("select", "vpp_status"), ("select", "vpp_remote_control"),
    ("select", "vpp_allow_ac_charging"), ("select", "limit_grid_export"),
    ("number", "vpp_time"), ("number", "vpp_power"), ("number", "grid_export_limit"),
    ("number", "ems_discharging_rate"), ("number", "ems_discharging_stop_soc_on_grid"),
    ("select", "time_1_enabled"), ("select", "time_1_mode"),
    ("sensor", "battery_soc"), ("sensor", "battery_charge_power"),
    ("sensor", "battery_discharge_power"), ("sensor", "total_forward_power"),
    ("sensor", "total_reverse_power"), ("sensor", "pv_power_total"),
    ("sensor", "total_load_power"),
}


def eid(platform, key):
    return f"{platform}.growatt_inverter_{HUB}_{key}"


class SolaxBackendTest(unittest.TestCase):
    def setUp(self):
        REGISTRY.by_uid.clear()
        ISSUES.clear()
        self.hass = FakeHass()
        self.hass.config_entries.entries.append(FakeEntry(
            "solax_modbus", "evcc",
            options={"name": HUB, "plugin": "growatt", "host": "192.168.21.4", "port": 1502,
                     "inverter_power_kw": 100}))
        for platform, key in KEYS:
            REGISTRY.add(platform, "solax_modbus", f"{HUB}_{key}", eid(platform, key))
        s = self.hass.states
        s.set(eid("select", "vpp_status"), "Enabled")
        s.set(eid("select", "vpp_remote_control"), "Disabled")
        s.set(eid("select", "limit_grid_export"), "Meter 1")
        s.set(eid("number", "vpp_power"), 0)
        s.set(eid("number", "ems_discharging_rate"), 100)
        s.set(eid("number", "ems_discharging_stop_soc_on_grid"), 10)
        s.set(eid("select", "time_1_enabled"), "Disabled")
        self.store = {}
        self.saves = 0
        self.config = {CONF_INVERTER_BACKEND: INVERTER_BACKEND_SOLAX,
                       CONF_SOLAX_HUB_NAME: HUB, CONF_INVERTER_RATED_KW: 10}

    def backend(self, dry=False):
        def save():
            self.saves += 1
        return SolaxModbusBackend(self.hass, self.config, make_actuator(self.hass, dry),
                                  lambda: self.store, save)

    def verify(self, sign=1, cycles=3):
        self.store[STORE_VERIFICATION] = {"backend": "solax_modbus", "hub": HUB,
                                          "plugin": "growatt", "sign": sign}
        self.store[STORE_OK_CYCLES] = cycles

    def test_discover(self):
        found = discover(self.hass, HUB)
        self.assertEqual(found["battery_soc_entity"], eid("sensor", "battery_soc"))
        self.assertEqual(found["foxess_grid_import_entity"], eid("sensor", "total_forward_power"))
        self.assertNotIn("cell_temp_entity", found)

    def test_blocked_until_verified(self):
        b = self.backend()
        self.assertIn("not verified", b.blocked_reason())
        run(b.async_force_charge(3.0))
        self.assertEqual(self.hass.services.calls, [])
        self.assertEqual(b.actuator.last_command.outcome, "blocked")
        # simulated intent keeps the control logic running
        self.assertTrue(b.is_force_charging())

    def test_force_charge_verified(self):
        self.verify()
        b = self.backend()
        run(b.async_force_charge(3.0))
        calls = [(c[0], c[2].get("entity_id"), c[2].get("option", c[2].get("value")))
                 for c in self.hass.services.calls]
        self.assertEqual(calls, [
            ("select", eid("select", "vpp_allow_ac_charging"), "Enabled"),
            ("number", eid("number", "vpp_power"), 30),
            ("number", eid("number", "vpp_time"), 15),
            ("select", eid("select", "vpp_remote_control"), "Enabled"),
        ])
        self.assertTrue(b.is_force_charging())
        run(b.async_set_charge_power(5.0))
        self.assertEqual(self.hass.services.calls[-1][2]["value"], 50)

    def test_commissioning_cap(self):
        self.verify(cycles=0)
        b = self.backend()
        run(b.async_force_discharge(9.0))
        self.assertIn(("number", "set_value",
                       {"entity_id": eid("number", "vpp_power"), "value": -50}),
                      self.hass.services.calls)

    def test_keepalive_and_self_use(self):
        self.verify()
        b = self.backend()
        run(b.async_force_discharge(4.0))
        n = len(self.hass.services.calls)
        run(b.async_tick(datetime.now(timezone.utc)))
        self.assertEqual(len(self.hass.services.calls), n)  # too early
        run(b.async_tick(datetime.now(timezone.utc) + timedelta(seconds=301)))
        self.assertGreater(len(self.hass.services.calls), n)
        self.assertEqual(self.hass.services.calls[-1][2]["option"], "Enabled")
        run(b.async_self_use())
        self.assertEqual(self.hass.services.calls[-1][2],
                         {"entity_id": eid("select", "vpp_remote_control"), "option": "Disabled"})
        n = len(self.hass.services.calls)
        run(b.async_tick(datetime.now(timezone.utc) + timedelta(seconds=900)))
        vpp_writes = [c for c in self.hass.services.calls[n:] if "vpp_power" in c[2]["entity_id"]]
        self.assertEqual(vpp_writes, [])  # no keep-alive in self use

    def test_mismatch_reassert_and_issue(self):
        self.verify()
        b = self.backend()
        run(b.async_force_charge(3.0))
        # another controller flips remote control off
        self.hass.states.set(eid("select", "vpp_remote_control"), "Disabled")
        later = datetime.now(timezone.utc) + timedelta(seconds=60)
        run(b.async_tick(later))
        self.assertEqual(self.hass.services.calls[-1][2],
                         {"entity_id": eid("select", "vpp_remote_control"), "option": "Enabled"})
        self.hass.states.set(eid("select", "vpp_remote_control"), "Disabled")
        run(b.async_tick(later + timedelta(seconds=60)))
        self.assertIn("inverter_write_mismatch", ISSUES)

    def test_capabilities(self):
        b = self.backend()
        # blocked (unverified) → still reports capabilities of the hardware
        caps = b.capabilities()
        self.assertTrue(caps.export_limit)
        self.assertTrue(caps.min_soc_backstop)
        self.assertTrue(caps.discharge_lock)
        self.hass.states.set(eid("number", "ems_discharging_rate"), "unavailable")
        self.assertFalse(b.capabilities().discharge_lock)

    def test_setup_issues(self):
        self.hass.config_entries.entries.append(FakeEntry("evcc_intg", "evcc"))
        self.hass.states.set(eid("select", "time_1_enabled"), "Enabled")
        self.hass.states.set(eid("select", "time_1_mode"), "Battery First")
        run(self.backend().async_setup())
        self.assertIn("evcc_present", ISSUES)
        self.assertIn("port 1502", ISSUES["evcc_present"]["translation_placeholders"]["reasons"])
        self.assertIn("inverter_competing_schedule", ISSUES)
        self.assertIn("inverter_not_verified", ISSUES)

    def test_dry_run_sends_nothing(self):
        self.verify()
        b = self.backend(dry=True)
        run(b.async_force_discharge(4.0))
        self.assertEqual(self.hass.services.calls, [])
        self.assertEqual(b.actuator.last_command.outcome, "dry_run")


class SelfTestTest(unittest.TestCase):
    def setUp(self):
        SolaxBackendTest.setUp(self)

    backend = SolaxBackendTest.backend

    def _run_test(self, readings, soc=50.0):
        b = self.backend()
        it = iter(readings)
        last = [readings[-1]]

        def net():
            try:
                last[0] = next(it)
            except StopIteration:
                pass
            return last[0]

        async def sleep(_):
            return None

        return b, run(b.async_self_test(net, lambda: soc, sleep))

    def test_positive_sign(self):
        b, res = self._run_test([-0.5, -0.5, -0.5, 0.8])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["sign"], 1)
        self.assertEqual(self.store[STORE_VERIFICATION]["sign"], 1)
        self.assertIsNone(b.blocked_reason())
        # always returns to self use
        self.assertEqual(self.hass.services.calls[-1][2]["option"], "Disabled")

    def test_negative_sign(self):
        _, res = self._run_test([0.0, 0.0, 0.0, -1.0])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["sign"], -1)

    def test_inconclusive(self):
        _, res = self._run_test([0.0, 0.0, 0.0, 0.1])
        self.assertFalse(res["ok"])
        self.assertNotIn(STORE_VERIFICATION, self.store)
        self.assertEqual(self.hass.services.calls[-1][2]["option"], "Disabled")

    def test_soc_out_of_range(self):
        _, res = self._run_test([0.0], soc=95)
        self.assertFalse(res["ok"])
        self.assertEqual(self.hass.services.calls, [])


if __name__ == "__main__":
    unittest.main()
