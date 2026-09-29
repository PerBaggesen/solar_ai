"""Forecast.Solar / Strømligning readers for HA integrations."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from _bootstrap import FakeEntry, FakeHass  # noqa: F401  (installs fakes)

from battery_arbitrage.ha_sources import (
    forecast_solar_rates,
    merge_watts_series,
    stromligning_ha_prices,
    stromligning_slot_key,
    tariff_schedule_from_prices,
)

CPH = ZoneInfo("Europe/Copenhagen")


def _entry(domain, title, entry_id, runtime_data=None, state="loaded"):
    e = FakeEntry(domain=domain, title=title)
    e.entry_id = entry_id
    e.state = SimpleNamespace(value=state)
    if runtime_data is not None:
        e.runtime_data = runtime_data
    return e


def _fs_coordinator(watts):
    return SimpleNamespace(data=SimpleNamespace(watts=watts))


class MergeWattsTest(unittest.TestCase):
    def test_aligned_series_sum(self):
        t0 = datetime(2026, 9, 25, 8, tzinfo=CPH)
        t1 = t0 + timedelta(hours=1)
        merged = merge_watts_series([{t0: 100, t1: 200}, {t0: 50, t1: 400}])
        self.assertEqual(merged, {t0: 150, t1: 600})

    def test_misaligned_series_interpolate(self):
        t0 = datetime(2026, 9, 25, 8, tzinfo=CPH)
        a = {t0: 0, t0 + timedelta(hours=2): 200}
        b = {t0 + timedelta(hours=1): 100}
        merged = merge_watts_series([a, b])
        # a interpolated to 100 W at t0+1h; b is 0 outside its single point
        self.assertAlmostEqual(merged[t0 + timedelta(hours=1)], 200)
        self.assertAlmostEqual(merged[t0], 0)
        self.assertAlmostEqual(merged[t0 + timedelta(hours=2)], 200)


class ForecastSolarTest(unittest.TestCase):
    def test_sums_loaded_planes_and_skips_others(self):
        hass = FakeHass()
        hass.data = {}
        t0 = datetime(2026, 9, 25, 10, tzinfo=CPH)
        hass.config_entries.entries = [
            _entry("forecast_solar", "Øst", "e1", _fs_coordinator({t0: 1000})),
            _entry("forecast_solar", "Vest", "e2", _fs_coordinator({t0: 500})),
            _entry("forecast_solar", "Broken", "e3", _fs_coordinator({t0: 9999}),
                   state="setup_retry"),
        ]
        data, used = forecast_solar_rates(hass, CPH)
        self.assertEqual(used, ["Øst", "Vest"])
        self.assertEqual(data["rates"], [
            {"start": t0.astimezone(timezone.utc).isoformat(), "value": 1500.0}])

    def test_legacy_hass_data_and_naive_strings(self):
        hass = FakeHass()
        hass.data = {"forecast_solar": {"e1": _fs_coordinator({"2026-09-25T12:00:00": 800})}}
        hass.config_entries.entries = [_entry("forecast_solar", "Øst", "e1")]
        data, _ = forecast_solar_rates(hass, CPH)
        self.assertEqual(data["rates"][0]["start"], "2026-09-25T10:00:00+00:00")

    def test_nothing_configured(self):
        hass = FakeHass()
        hass.data = {}
        self.assertEqual(forecast_solar_rates(hass, CPH), ({}, []))


def _sl_price(dt, dist, net, syst, total=2.0, forecast=False):
    return {
        "date": dt,
        "price": {"value": total / 1.25, "total": total},
        "details": {
            "electricity": {"value": 0.5},
            "distribution": {"value": dist},
            "transmission": {"netTariff": {"value": net}, "systemTariff": {"value": syst}},
        },
        "forecast": forecast,
    }


class StromligningTest(unittest.TestCase):
    def _hass(self, today, tomorrow=()):
        hass = FakeHass()
        api = SimpleNamespace(prices_today=list(today), prices_tomorrow=list(tomorrow))
        hass.data = {"stromligning": {"s1": api}}
        hass.config_entries.entries = [_entry("stromligning", "Strømligning", "s1")]
        return hass

    def test_keys_and_forecast_skip(self):
        t = datetime(2026, 9, 25, 14, 15, tzinfo=CPH)
        hass = self._hass([_sl_price(t, 0.1, 0.05, 0.07)],
                          [_sl_price(t + timedelta(days=1), 0.1, 0.05, 0.07, forecast=True)])
        prices = stromligning_ha_prices(hass, CPH)
        self.assertEqual(list(prices), ["2026-09-25T12:15:00.000Z"])
        self.assertEqual(prices["2026-09-25T12:15:00.000Z"]["date"], "2026-09-25T12:15:00+00:00")
        self.assertEqual(stromligning_slot_key(t + timedelta(minutes=7)), "2026-09-25T12:15:00.000Z")

    def test_not_loaded(self):
        hass = FakeHass()
        hass.data = {}
        self.assertIsNone(stromligning_ha_prices(hass, CPH))

    def test_tariff_schedule(self):
        day = datetime(2026, 9, 25, tzinfo=CPH)
        today = []
        for q in range(96):
            dt = day + timedelta(minutes=15 * q)
            dist = 0.3 if 17 <= dt.hour < 21 else 0.1
            today.append(_sl_price(dt, dist, 0.05, 0.07))
        prices = stromligning_ha_prices(self._hass(today), CPH)
        sched = tariff_schedule_from_prices(prices, CPH, day + timedelta(hours=12))
        self.assertEqual(len(sched), 24)
        self.assertAlmostEqual(sched[0], 0.22)
        self.assertAlmostEqual(sched[18], 0.42)

    def test_tariff_schedule_incomplete(self):
        day = datetime(2026, 9, 25, 12, tzinfo=CPH)
        prices = stromligning_ha_prices(self._hass([_sl_price(day, 0.1, 0.05, 0.07)]), CPH)
        self.assertIsNone(tariff_schedule_from_prices(prices, CPH, day))


if __name__ == "__main__":
    unittest.main()
