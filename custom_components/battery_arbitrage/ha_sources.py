"""Read forecast and price data from other Home Assistant integrations.

Solar AI can take its solar forecast and its retail prices from integrations
the user already runs in Home Assistant, instead of calling the providers'
APIs itself:

- Forecast.Solar (core `forecast_solar`): the per-timestamp `watts` estimate
  lives on each config entry's coordinator. Older HA releases exposed it as a
  `watts` sensor attribute, current ones don't, so the coordinator is the only
  reliable place to read it. Every loaded entry (one per panel plane) is
  summed, the same way the Energy dashboard combines them.
- Strømligning (custom `stromligning` by MTrab): the per-slot price entries
  with their full breakdown live on the API object in `hass.data`. They have
  the same shape as the entries Solar AI's own Strømligning client returns,
  so they drop straight into the coordinator's price cache.

The functions only touch `hass.config_entries` and `hass.data`, so they are
easy to unit test with a fake hass.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, tzinfo
from typing import Any

_LOGGER = logging.getLogger(__name__)

FORECAST_SOLAR_DOMAIN = "forecast_solar"
STROMLIGNING_DOMAIN = "stromligning"


def _to_aware(ts: Any, default_tz: tzinfo) -> datetime | None:
    """Parse a datetime or ISO string; naive values get `default_tz`."""
    if isinstance(ts, datetime):
        dt = ts
    else:
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)
    return dt


def _is_loaded(entry: Any) -> bool:
    if getattr(entry, "disabled_by", None):
        return False
    state = getattr(entry, "state", None)
    if state is None:
        return True
    # ConfigEntryState is a str-valued enum ("loaded", "setup_retry", ...)
    return str(getattr(state, "value", state)) == "loaded"


# ─────────────────────────────────────────────────────────────────────────────
# Forecast.Solar
# ─────────────────────────────────────────────────────────────────────────────

def merge_watts_series(series: list[dict[datetime, float]]) -> dict[datetime, float]:
    """Sum several power series whose timestamps may not line up.

    Each series is linearly interpolated onto the union of all timestamps and
    counts as 0 W outside its own first..last range (Forecast.Solar reports
    0 W at sunrise and sunset anyway).
    """
    series = [s for s in series if s]
    if not series:
        return {}
    if len(series) == 1:
        return dict(series[0])

    all_ts = sorted({ts for s in series for ts in s})
    total = {ts: 0.0 for ts in all_ts}
    for s in series:
        points = sorted(s.items())
        i = 0
        for ts in all_ts:
            if ts < points[0][0] or ts > points[-1][0]:
                continue
            while i + 1 < len(points) and points[i + 1][0] <= ts:
                i += 1
            t0, v0 = points[i]
            if t0 == ts or i + 1 >= len(points):
                total[ts] += v0
                continue
            t1, v1 = points[i + 1]
            frac = (ts - t0).total_seconds() / (t1 - t0).total_seconds()
            total[ts] += v0 + (v1 - v0) * frac
    return total


def _forecast_solar_entry_watts(hass: Any, entry: Any, default_tz: tzinfo) -> dict[datetime, float]:
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is None:
        coordinator = (getattr(hass, "data", {}) or {}).get(FORECAST_SOLAR_DOMAIN, {}).get(
            getattr(entry, "entry_id", None))
    estimate = getattr(coordinator, "data", None)
    watts = getattr(estimate, "watts", None)
    if not isinstance(watts, dict):
        return {}
    out: dict[datetime, float] = {}
    for ts, value in watts.items():
        dt = _to_aware(ts, default_tz)
        if dt is None:
            continue
        try:
            out[dt] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def forecast_solar_rates(hass: Any, default_tz: tzinfo) -> tuple[dict, list[str]]:
    """Summed Forecast.Solar estimate across all loaded entries.

    Returns ({"rates": [{"start": utc-iso, "value": W}, ...]} or {}, titles
    of the entries that contributed).
    """
    series: list[dict[datetime, float]] = []
    used: list[str] = []
    for entry in hass.config_entries.async_entries(FORECAST_SOLAR_DOMAIN):
        if not _is_loaded(entry):
            continue
        watts = _forecast_solar_entry_watts(hass, entry, default_tz)
        if watts:
            series.append(watts)
            used.append(getattr(entry, "title", "") or getattr(entry, "entry_id", "?"))
    merged = merge_watts_series(series)
    rates = [
        {"start": ts.astimezone(timezone.utc).isoformat(), "value": round(v, 1)}
        for ts, v in sorted(merged.items())
    ]
    return ({"rates": rates} if rates else {}), used


# ─────────────────────────────────────────────────────────────────────────────
# Strømligning
# ─────────────────────────────────────────────────────────────────────────────

def stromligning_slot_key(dt: datetime) -> str:
    """Canonical cache key: UTC, 15-min aligned, `.000Z` suffix.

    Must match `stromligning.fetch_prices` and the coordinator/sensor lookups.
    """
    dt_utc = dt.astimezone(timezone.utc)
    return dt_utc.replace(
        minute=(dt_utc.minute // 15) * 15, second=0, microsecond=0,
    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def stromligning_ha_prices(hass: Any, default_tz: tzinfo) -> dict[str, dict] | None:
    """Per-slot price entries from the HA Strømligning integration.

    Returns None when the integration isn't loaded or has no data. Only
    today's and tomorrow's published prices are used; Strømligning's
    speculative forecast entries are skipped.
    """
    domain_data = (getattr(hass, "data", {}) or {}).get(STROMLIGNING_DOMAIN) or {}
    for entry in hass.config_entries.async_entries(STROMLIGNING_DOMAIN):
        if not _is_loaded(entry):
            continue
        api = domain_data.get(getattr(entry, "entry_id", None))
        if api is None:
            continue
        entries = list(getattr(api, "prices_today", None) or []) + list(
            getattr(api, "prices_tomorrow", None) or [])
        out: dict[str, dict] = {}
        for price in entries:
            if not isinstance(price, dict) or price.get("forecast"):
                continue
            dt = _to_aware(price.get("date"), default_tz)
            if dt is None:
                continue
            item = dict(price)
            # The HA integration replaces `date` with a local datetime; store
            # an ISO string so the cache stays JSON-serialisable.
            item["date"] = dt.astimezone(timezone.utc).isoformat()
            out[stromligning_slot_key(dt)] = item
        if out:
            return out
    return None


def tariff_schedule_from_prices(
    prices: dict[str, dict], local_tz: tzinfo, day: datetime,
) -> list[float] | None:
    """24 hourly network tariffs (DKK/kWh ex VAT) for `day` in `local_tz`.

    Tariff = DSO distribution + Energinet transmission (netTariff) + Energinet
    system tariff, the same components the Datahub fetch sums. Quarter-hour
    slots are averaged per hour. Returns None unless all 24 hours are covered.
    """
    target = day.astimezone(local_tz).date()
    buckets: dict[int, list[float]] = {}
    for key, entry in prices.items():
        try:
            ts = datetime.strptime(key[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        local = ts.astimezone(local_tz)
        if local.date() != target:
            continue
        details = entry.get("details") or {}
        transmission = details.get("transmission") or {}
        try:
            value = (
                float((details.get("distribution") or {}).get("value", 0.0))
                + float((transmission.get("netTariff") or {}).get("value", 0.0))
                + float((transmission.get("systemTariff") or {}).get("value", 0.0))
            )
        except (TypeError, ValueError):
            continue
        buckets.setdefault(local.hour, []).append(value)
    # DST days have 23 or 25 hours; every wall-clock hour 0..23 except the
    # skipped one still needs a value, so borrow the previous hour for gaps
    # of one.
    schedule: list[float] = []
    for hour in range(24):
        vals = buckets.get(hour)
        if vals:
            schedule.append(round(sum(vals) / len(vals), 4))
        elif schedule and hour - 1 in buckets:
            schedule.append(schedule[-1])
        else:
            return None
    return schedule
