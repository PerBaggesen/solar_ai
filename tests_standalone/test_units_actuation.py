"""Unit conversion and actuation-layer tests."""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from _bootstrap import FakeHass, make_actuator  # noqa: F401  (installs fakes)

from battery_arbitrage.actuation import (
    OUTCOME_BLOCKED,
    OUTCOME_DRY_RUN,
    OUTCOME_FAILED,
    OUTCOME_SENT,
    Actuator,
    WriteVerifier,
    values_match,
)
from battery_arbitrage.units import energy_to_kwh, power_to_kw

T0 = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


class UnitsTest(unittest.TestCase):
    def test_power(self):
        self.assertAlmostEqual(power_to_kw(2500, "W"), 2.5)
        self.assertAlmostEqual(power_to_kw(2.5, "kW"), 2.5)
        self.assertAlmostEqual(power_to_kw(0.002, "MW"), 2.0)
        # Unknown / missing unit: pass through (FoxESS historical behaviour)
        self.assertAlmostEqual(power_to_kw(2.5, None), 2.5)
        self.assertAlmostEqual(power_to_kw(2.5, "A"), 2.5)

    def test_energy(self):
        self.assertAlmostEqual(energy_to_kwh(1500, "Wh"), 1.5)
        self.assertAlmostEqual(energy_to_kwh(1.5, "kWh"), 1.5)
        self.assertAlmostEqual(energy_to_kwh(1.5, None), 1.5)


class ActuatorTest(unittest.TestCase):
    def _actuator(self, dry=False, fail=False):
        calls = []

        async def call(domain, service, data, context):
            if fail:
                raise RuntimeError("boom")
            calls.append((domain, service, data, context))

        class Ctx:
            _n = 0

            def __init__(self):
                Ctx._n += 1
                self.id = f"ctx{Ctx._n}"

        return Actuator(call, context_factory=Ctx, dry_run=lambda: dry), calls

    def test_sent(self):
        act, calls = self._actuator()
        sent = asyncio.run(act.async_write("number", "set_value", {"entity_id": "n.x", "value": 1}))
        self.assertTrue(sent)
        self.assertEqual(len(calls), 1)
        self.assertEqual(act.last_command.outcome, OUTCOME_SENT)
        self.assertTrue(act.is_own_context(calls[0][3].id))
        self.assertFalse(act.is_own_context("other"))

    def test_dry_run_never_calls(self):
        act, calls = self._actuator(dry=True)
        sent = asyncio.run(act.async_write("number", "set_value", {"entity_id": "n.x", "value": 1}))
        self.assertFalse(sent)
        self.assertEqual(calls, [])
        self.assertEqual(act.last_command.outcome, OUTCOME_DRY_RUN)

    def test_blocked_never_calls(self):
        act, calls = self._actuator()
        sent = asyncio.run(act.async_write("select", "select_option", {"entity_id": "s.x"},
                                           blocked_reason="not verified"))
        self.assertFalse(sent)
        self.assertEqual(calls, [])
        self.assertEqual(act.last_command.outcome, OUTCOME_BLOCKED)
        self.assertEqual(act.last_command.detail, "not verified")

    def test_failure_recorded_and_raised(self):
        act, _ = self._actuator(fail=True)
        with self.assertRaises(RuntimeError):
            asyncio.run(act.async_write("number", "set_value", {"entity_id": "n.x"}))
        self.assertEqual(act.last_command.outcome, OUTCOME_FAILED)


class VerifierTest(unittest.TestCase):
    def test_values_match(self):
        self.assertTrue(values_match(50, "50.0"))
        self.assertTrue(values_match(100, "99.6"))
        self.assertFalse(values_match(100, "95"))
        self.assertTrue(values_match("Enabled", "enabled"))
        self.assertFalse(values_match("Enabled", "Disabled"))

    def test_grace_match_mismatch_escalation(self):
        v = WriteVerifier(grace_s=45, fail_threshold=2, reassert_interval_s=600)
        state = {"select.remote": "Enabled"}
        v.expect("select.remote", "Enabled", T0)
        # within grace: nothing checked
        state["select.remote"] = "Disabled"
        self.assertEqual(v.evaluate(state.get, T0 + timedelta(seconds=10)), [])
        # first mismatch: re-assert, not yet persistent
        m = v.evaluate(state.get, T0 + timedelta(seconds=60))
        self.assertEqual(len(m), 1)
        self.assertTrue(m[0].reassert)
        self.assertFalse(m[0].persistent)
        # second mismatch shortly after: persistent, but re-assert rate-limited
        m = v.evaluate(state.get, T0 + timedelta(seconds=120))
        self.assertTrue(m[0].persistent)
        self.assertFalse(m[0].reassert)
        # after the interval, re-assert again
        m = v.evaluate(state.get, T0 + timedelta(seconds=700))
        self.assertTrue(m[0].reassert)
        # value restored → cleared
        state["select.remote"] = "Enabled"
        self.assertEqual(v.evaluate(state.get, T0 + timedelta(seconds=800)), [])

    def test_unavailable_not_counted(self):
        v = WriteVerifier(grace_s=0)
        v.expect("number.p", 50, T0)
        self.assertEqual(v.evaluate({"number.p": "unavailable"}.get, T0 + timedelta(seconds=5)), [])


if __name__ == "__main__":
    unittest.main()
