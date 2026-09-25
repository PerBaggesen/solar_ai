"""Test bootstrap: import Solar AI modules without a Home Assistant install.

Registers `battery_arbitrage` as a bare namespace (so its HA-heavy
`__init__.py` is not executed) and installs minimal fakes for the two HA
helper modules the inverter backends use (entity_registry, issue_registry).
Also provides FakeHass: states, service-call recording, config entries.

Run from the integration's parent directory:
    python3 -m unittest discover -s battery_arbitrage/tests -t battery_arbitrage/tests
"""
from __future__ import annotations

import pathlib
import sys
import types
from dataclasses import dataclass, field
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG = "battery_arbitrage"


def _install_package() -> None:
    if PKG not in sys.modules:
        pkg = types.ModuleType(PKG)
        pkg.__path__ = [str(ROOT)]
        sys.modules[PKG] = pkg


# ---------------------------------------------------------------------------
# Fake HA helpers
# ---------------------------------------------------------------------------

class FakeEntityRegistry:
    def __init__(self) -> None:
        self.by_uid: dict[tuple[str, str, str], str] = {}

    def add(self, platform: str, integration: str, unique_id: str, entity_id: str) -> None:
        self.by_uid[(platform, integration, unique_id)] = entity_id

    def async_get_entity_id(self, platform: str, integration: str, unique_id: str):
        return self.by_uid.get((platform, integration, unique_id))


REGISTRY = FakeEntityRegistry()
ISSUES: dict[str, dict] = {}


def _install_ha_fakes() -> None:
    if "homeassistant.helpers.entity_registry" in sys.modules:
        return
    ha = types.ModuleType("homeassistant")
    helpers = types.ModuleType("homeassistant.helpers")
    ha.__path__ = []
    helpers.__path__ = []
    er = types.ModuleType("homeassistant.helpers.entity_registry")
    er.async_get = lambda hass: REGISTRY
    ir = types.ModuleType("homeassistant.helpers.issue_registry")

    class IssueSeverity:
        WARNING = "warning"
        ERROR = "error"

    def async_create_issue(hass, domain, issue_id, **kwargs):
        ISSUES[issue_id] = kwargs

    def async_delete_issue(hass, domain, issue_id):
        ISSUES.pop(issue_id, None)

    ir.IssueSeverity = IssueSeverity
    ir.async_create_issue = async_create_issue
    ir.async_delete_issue = async_delete_issue
    helpers.entity_registry = er
    helpers.issue_registry = ir
    ha.helpers = helpers
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    ha.core = core
    sys.modules.update({
        "homeassistant": ha,
        "homeassistant.core": core,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.entity_registry": er,
        "homeassistant.helpers.issue_registry": ir,
    })


_install_package()
_install_ha_fakes()


# ---------------------------------------------------------------------------
# Fake hass
# ---------------------------------------------------------------------------

@dataclass
class FakeState:
    state: str
    attributes: dict = field(default_factory=dict)


class FakeStates:
    def __init__(self) -> None:
        self._s: dict[str, FakeState] = {}

    def set(self, entity_id: str, state: Any, **attrs: Any) -> None:
        self._s[entity_id] = FakeState(str(state), attrs)

    def get(self, entity_id: str):
        return self._s.get(entity_id)


@dataclass
class FakeEntry:
    domain: str
    title: str
    data: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)
    disabled_by: Any = None


class FakeConfigEntries:
    def __init__(self) -> None:
        self.entries: list[FakeEntry] = []

    def async_entries(self, domain: str):
        return [e for e in self.entries if e.domain == domain]


class FakeServices:
    def __init__(self, states: FakeStates) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.responses: dict[tuple[str, str], Any] = {}
        self._states = states
        # When True, select/number writes update the fake state, as the real
        # solax_modbus entities do.
        self.reflect = True

    async def async_call(self, domain, service, data, blocking=False,
                         context=None, return_response=False):
        self.calls.append((domain, service, dict(data)))
        if self.reflect and "entity_id" in data:
            if domain == "select" and service == "select_option":
                self._states.set(data["entity_id"], data["option"])
            elif domain == "number" and service == "set_value":
                self._states.set(data["entity_id"], data["value"])
        return self.responses.get((domain, service))


class FakeHass:
    def __init__(self) -> None:
        self.states = FakeStates()
        self.services = FakeServices(self.states)
        self.config_entries = FakeConfigEntries()


def make_actuator(hass: FakeHass, dry_run: bool = False):
    from battery_arbitrage.actuation import Actuator

    async def _call(domain, service, data, context):
        return await hass.services.async_call(domain, service, data, blocking=True,
                                              context=context)

    return Actuator(_call, dry_run=lambda: dry_run)
