"""Dashboard entity remapping."""
from __future__ import annotations

import unittest

import _bootstrap  # noqa: F401

from battery_arbitrage.dashboard_setup import rebase_view_links, remap_entities


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

    def test_prefix_id_not_rewritten_inside_longer_id(self):
        # The bundled YAML has both ..._24h and ..._24h_justeret; a template
        # holding the longer one must survive a rewrite of the shorter one.
        cfg = {"content": "{{ states('sensor.x_prognose_24h_justeret') }}"
                          " / {{ states('sensor.x_prognose_24h') }}"}
        out = remap_entities(cfg, {"sensor.x_prognose_24h": "sensor.y_24h"})
        self.assertEqual(
            out["content"],
            "{{ states('sensor.x_prognose_24h_justeret') }} / {{ states('sensor.y_24h') }}")

    def test_states_attribute_form(self):
        cfg = {"content": "{{ states.sensor.old_info.attributes.x }}"}
        out = remap_entities(cfg, {"sensor.old_info": "sensor.new_info"})
        self.assertEqual(out["content"], "{{ states.sensor.new_info.attributes.x }}")

    def test_no_chained_rewrites(self):
        out = remap_entities("sensor.a sensor.b", {"sensor.a": "sensor.b", "sensor.b": "sensor.c"})
        self.assertEqual(out, "sensor.b sensor.c")

    def test_empty_mapping_is_identity(self):
        cfg = {"a": "sensor.foxessmodbus_feed_in"}
        self.assertEqual(remap_entities(cfg, {}), cfg)


class ViewLinkTest(unittest.TestCase):
    def test_links_follow_dashboard_url(self):
        cfg = {"cards": [
            {"path": "/battery-arbitrage/logs"},
            {"prices_navigate_path": "/battery-arbitrage/priser"},
            {"path": "/battery-arbitrage"},
            {"path": "/config/integrations/integration/battery_arbitrage"},
            {"path": "/x/battery-arbitrage/y"},
            {"path": "logs"},
        ]}
        out = rebase_view_links(cfg, "solar-ai")["cards"]
        self.assertEqual(out[0]["path"], "/solar-ai/logs")
        self.assertEqual(out[1]["prices_navigate_path"], "/solar-ai/priser")
        self.assertEqual(out[2]["path"], "/solar-ai")
        self.assertEqual(out[3]["path"], "/config/integrations/integration/battery_arbitrage")
        self.assertEqual(out[4]["path"], "/x/battery-arbitrage/y")
        self.assertEqual(out[5]["path"], "logs")

    def test_bundled_url_is_identity(self):
        cfg = {"path": "/battery-arbitrage/logs"}
        self.assertEqual(rebase_view_links(cfg, "battery-arbitrage"), cfg)


if __name__ == "__main__":
    unittest.main()
