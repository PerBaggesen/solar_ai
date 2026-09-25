"""Dashboard entity remapping."""
from __future__ import annotations

import unittest

import _bootstrap  # noqa: F401

from battery_arbitrage.dashboard_setup import remap_entities


class RemapTest(unittest.TestCase):
    def test_whole_ids_only(self):
        cfg = {"cards": [
            {"entity": "sensor.foxessmodbus_battery_charge"},
            {"entity": "sensor.foxessmodbus_battery_charge_total"},
            {"content": "{{ states('sensor.foxessmodbus_battery_charge') }} kW"},
            {"entities": ["sensor.foxessmodbus_feed_in", 5, None]},
        ]}
        out = remap_entities(cfg, {
            "sensor.foxessmodbus_battery_charge": "sensor.growatt_battery_charge_power",
            "sensor.foxessmodbus_feed_in": "sensor.growatt_export",
        })
        self.assertEqual(out["cards"][0]["entity"], "sensor.growatt_battery_charge_power")
        self.assertEqual(out["cards"][1]["entity"], "sensor.foxessmodbus_battery_charge_total")
        self.assertIn("sensor.growatt_battery_charge_power", out["cards"][2]["content"])
        self.assertEqual(out["cards"][3]["entities"], ["sensor.growatt_export", 5, None])

    def test_empty_mapping_is_identity(self):
        cfg = {"a": "sensor.foxessmodbus_feed_in"}
        self.assertEqual(remap_entities(cfg, {}), cfg)


if __name__ == "__main__":
    unittest.main()
