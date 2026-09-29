"""Growatt VPP profile: pure command-planning tests."""
from __future__ import annotations

import unittest

import _bootstrap  # noqa: F401  (installs package + fakes)

from battery_arbitrage.inverters.profiles import get_profile
from battery_arbitrage.inverters.profiles.base import (
    INTENT_FORCE_CHARGE,
    INTENT_FORCE_DISCHARGE,
    INTENT_SELF_USE,
    ProfileContext,
    Write,
)
from battery_arbitrage.inverters.profiles.growatt import (
    GrowattVppProfile,
    export_limit_pct,
    kw_to_vpp_pct,
    watchdog_minutes,
)


def ctx(states=None, **kw):
    states = states or {"vpp_status": "Enabled"}
    return ProfileContext(rated_kw=kw.pop("rated_kw", 10.0), state=states.get, **kw)


class ConversionTest(unittest.TestCase):
    def test_kw_to_pct(self):
        self.assertEqual(kw_to_vpp_pct(3.0, 10.0), 30)
        self.assertEqual(kw_to_vpp_pct(3.2, 10.0), 30)
        self.assertEqual(kw_to_vpp_pct(3.3, 10.0), 35)
        self.assertEqual(kw_to_vpp_pct(0.1, 10.0), 5)   # never rounds a request to 0
        self.assertEqual(kw_to_vpp_pct(0.0, 10.0), 0)
        self.assertEqual(kw_to_vpp_pct(25.0, 10.0), 100)
        self.assertEqual(kw_to_vpp_pct(8.0, 10.0, max_pct=50), 50)

    def test_watchdog(self):
        self.assertEqual(watchdog_minutes(15), 15)
        self.assertEqual(watchdog_minutes(1), 5)
        self.assertEqual(watchdog_minutes(17), 15)
        self.assertEqual(watchdog_minutes(5000), 1440)

    def test_export_pct(self):
        self.assertEqual(export_limit_pct(10000, 10.0), 100)
        self.assertEqual(export_limit_pct(25, 10.0), 0)
        self.assertEqual(export_limit_pct(0, 10.0), 0)
        self.assertEqual(export_limit_pct(5000, 10.0), 50)


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.p = get_profile("growatt")
        self.assertIsInstance(self.p, GrowattVppProfile)

    def test_force_charge_order_and_sign(self):
        w = self.p.plan_force_charge(3.0, ctx())
        keys = [x.key for x in w]
        # power + timeout written before remote control is enabled
        self.assertLess(keys.index("vpp_power"), keys.index("vpp_remote_control"))
        self.assertLess(keys.index("vpp_time"), keys.index("vpp_remote_control"))
        self.assertIn(Write("select", "vpp_allow_ac_charging", "Enabled"), w)
        self.assertIn(Write("number", "vpp_power", 30), w)
        self.assertEqual(w[-1], Write("select", "vpp_remote_control", "Enabled"))
        # vpp_status already enabled → not rewritten
        self.assertNotIn("vpp_status", keys)

    def test_inverted_sign(self):
        w = self.p.plan_force_charge(3.0, ctx(sign=-1))
        self.assertIn(Write("number", "vpp_power", -30), w)
        w = self.p.plan_force_discharge(3.0, ctx(sign=-1))
        self.assertIn(Write("number", "vpp_power", 30), w)

    def test_enables_vpp_status_when_off(self):
        w = self.p.plan_force_charge(3.0, ctx({"vpp_status": "Disabled"}))
        self.assertEqual(w[0], Write("select", "vpp_status", "Enabled"))

    def test_force_charge_mode_only_holds(self):
        w = self.p.plan_force_charge(None, ctx())
        self.assertIn(Write("number", "vpp_power", 0), w)

    def test_force_discharge(self):
        w = self.p.plan_force_discharge(5.0, ctx())
        self.assertIn(Write("number", "vpp_power", -50), w)
        self.assertIn(Write("select", "vpp_allow_ac_charging", "Disabled"), w)
        # kw <= 0 → full (capped) rate
        w = self.p.plan_force_discharge(0, ctx(max_pct=50))
        self.assertIn(Write("number", "vpp_power", -50), w)

    def test_self_use(self):
        self.assertEqual(self.p.plan_self_use(ctx()),
                         [Write("select", "vpp_remote_control", "Disabled")])

    def test_keepalive(self):
        c = ctx(watchdog_min=15)
        self.assertEqual(self.p.keepalive_interval_s(c), 300)
        self.assertEqual(self.p.plan_keepalive(INTENT_SELF_USE, None, c), [])
        w = self.p.plan_keepalive(INTENT_FORCE_DISCHARGE, -50, c)
        self.assertIn(Write("number", "vpp_power", -50), w)
        self.assertIn(Write("select", "vpp_remote_control", "Enabled"), w)
        w = self.p.plan_keepalive(INTENT_FORCE_CHARGE, 30, c)
        self.assertIn(Write("select", "vpp_allow_ac_charging", "Enabled"), w)

    def test_is_force_charging(self):
        self.assertTrue(self.p.is_force_charging(
            ctx({"vpp_remote_control": "Enabled", "vpp_power": "30"})))
        self.assertFalse(self.p.is_force_charging(
            ctx({"vpp_remote_control": "Enabled", "vpp_power": "-30"})))
        self.assertTrue(self.p.is_force_charging(
            ctx({"vpp_remote_control": "Enabled", "vpp_power": "-30"}, sign=-1)))
        self.assertFalse(self.p.is_force_charging(
            ctx({"vpp_remote_control": "Disabled", "vpp_power": "30"})))

    def test_export_limit(self):
        self.assertFalse(self.p.export_limit_supported(ctx({"limit_grid_export": "Disabled"})))
        self.assertFalse(self.p.export_limit_supported(ctx({})))
        self.assertTrue(self.p.export_limit_supported(ctx({"limit_grid_export": "Meter 1"})))
        self.assertEqual(self.p.plan_export_limit(25, ctx()),
                         [Write("number", "grid_export_limit", 0)])

    def test_competing_controls(self):
        states = {"time_1_enabled": "Enabled", "time_1_mode": "Battery First",
                  "time_2_enabled": "Enabled", "time_2_mode": "Load First",
                  "time_3_enabled": "Disabled", "time_3_mode": "Grid First"}
        self.assertEqual(self.p.competing_controls(ctx(states)),
                         ["time slot 1 (Battery First)"])

    def test_verify_keys_exclude_countdown(self):
        self.assertNotIn("vpp_time", self.p.verify_keys())
        self.assertIn("vpp_remote_control", self.p.verify_keys())

    def test_unknown_plugin(self):
        self.assertIsNone(get_profile("nope"))
        self.assertFalse(get_profile("solax").control_supported)


if __name__ == "__main__":
    unittest.main()
