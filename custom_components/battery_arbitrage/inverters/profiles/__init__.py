"""Registry of solax_modbus plugin profiles (plugin name → profile)."""
from __future__ import annotations

from .base import PluginProfile
from .growatt import GrowattVppProfile
from .solax import SolaxProfile

PROFILES: dict[str, type[PluginProfile]] = {
    GrowattVppProfile.plugin: GrowattVppProfile,
    SolaxProfile.plugin: SolaxProfile,
}


def get_profile(plugin: str | None) -> PluginProfile | None:
    cls = PROFILES.get((plugin or "").lower())
    return cls() if cls else None
