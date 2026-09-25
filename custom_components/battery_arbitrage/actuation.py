"""Actuation layer shared by every inverter / charger backend.

Every write Solar AI makes to hardware-facing entities goes through an
`Actuator`, which provides three safety nets:

  * **Dry run** — when enabled, writes are logged and recorded (see
    `last_command` / `history`) but never sent. Used to commission a new
    backend: watch what Solar AI *would* do before letting it act.
  * **Blocked writes** — a backend can refuse to actuate (e.g. an inverter
    whose control direction has not been verified by the self-test yet).
    Blocked writes are recorded like dry-run writes, with the reason.
  * **Context tracking** — each sent write carries its own HA `Context`, so
    state changes caused by Solar AI can be told apart from changes made by
    other automations, the inverter app, or another energy manager.

`WriteVerifier` complements this: it remembers the value Solar AI last wrote
to each entity and, on later ticks, reports entities whose state no longer
matches (a failed write, or another controller overriding us).

Pure Python — the HA service call and context factory are injected, so this
module has no Home Assistant imports and is unit-testable standalone.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

_LOGGER = logging.getLogger(__name__)

# (domain, service, data, context) -> awaitable service response
ServiceCaller = Callable[[str, str, dict, Any], Awaitable[Any]]

OUTCOME_SENT = "sent"
OUTCOME_DRY_RUN = "dry_run"
OUTCOME_BLOCKED = "blocked"
OUTCOME_FAILED = "failed"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class CommandRecord:
    """One write attempt, as exposed on the diagnostics sensor."""

    ts: datetime
    domain: str
    service: str
    data: dict
    outcome: str
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "time": self.ts.isoformat(),
            "service": f"{self.domain}.{self.service}",
            "data": dict(self.data),
            "outcome": self.outcome,
            "detail": self.detail,
        }


class Actuator:
    """Gatekeeper for all hardware-facing service calls."""

    HISTORY_LEN = 25
    OWN_CONTEXTS_LEN = 500

    def __init__(
        self,
        call_service: ServiceCaller,
        *,
        context_factory: Callable[[], Any] | None = None,
        dry_run: Callable[[], bool] = lambda: False,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._call = call_service
        self._context_factory = context_factory
        self._dry_run = dry_run
        self._now = now
        self.history: deque[CommandRecord] = deque(maxlen=self.HISTORY_LEN)
        self._own_context_ids: deque[str] = deque(maxlen=self.OWN_CONTEXTS_LEN)

    @property
    def dry_run(self) -> bool:
        return bool(self._dry_run())

    @property
    def last_command(self) -> CommandRecord | None:
        return self.history[-1] if self.history else None

    def is_own_context(self, context_id: str | None) -> bool:
        return context_id is not None and context_id in self._own_context_ids

    def _record(self, domain: str, service: str, data: dict,
                outcome: str, detail: str = "") -> CommandRecord:
        rec = CommandRecord(self._now(), domain, service, dict(data), outcome, detail)
        self.history.append(rec)
        return rec

    async def async_write(
        self,
        domain: str,
        service: str,
        data: dict,
        *,
        blocked_reason: str | None = None,
    ) -> bool:
        """Send a write, unless dry run is on or the caller says it is blocked.

        Returns True only when the call was actually sent. Dry-run and blocked
        writes return False; callers treat them as "simulated" and carry on, so
        the control logic runs exactly as it would live. Exceptions from the
        service call are recorded and re-raised, preserving each caller's own
        error handling.
        """
        if self.dry_run:
            self._record(domain, service, data, OUTCOME_DRY_RUN)
            _LOGGER.info("[dry run] %s.%s %s", domain, service, data)
            return False
        if blocked_reason:
            self._record(domain, service, data, OUTCOME_BLOCKED, blocked_reason)
            _LOGGER.info("[blocked: %s] %s.%s %s", blocked_reason, domain, service, data)
            return False
        context = self._context_factory() if self._context_factory else None
        if context is not None and getattr(context, "id", None):
            self._own_context_ids.append(context.id)
        try:
            await self._call(domain, service, data, context)
        except Exception as err:
            self._record(domain, service, data, OUTCOME_FAILED, str(err))
            raise
        self._record(domain, service, data, OUTCOME_SENT)
        return True

    async def async_read(self, domain: str, service: str, data: dict) -> Any:
        """Service call that only reads (e.g. a register read). Never gated."""
        return await self._call(domain, service, data, None)


# ---------------------------------------------------------------------------
# Write verification
# ---------------------------------------------------------------------------

_UNAVAILABLE = (None, "unknown", "unavailable", "")


def values_match(expected: Any, actual: Any) -> bool:
    """Compare a written value with an entity state.

    Numbers match within max(0.51, 1 %) — entity steps/scaling (e.g. a %
    register with 0.1 resolution) mean exact equality is too strict. Anything
    else is compared as a case-insensitive string.
    """
    try:
        e = float(expected)
        a = float(actual)
    except (TypeError, ValueError):
        return str(expected).strip().lower() == str(actual).strip().lower()
    return abs(e - a) <= max(0.51, abs(e) * 0.01)


@dataclass
class _Expectation:
    value: Any
    written_at: datetime
    mismatches: int = 0
    last_reassert: datetime | None = None


@dataclass
class Mismatch:
    entity_id: str
    expected: Any
    actual: Any
    count: int
    reassert: bool
    persistent: bool


@dataclass
class WriteVerifier:
    """Tracks the last value written to each entity and reports drift.

    `grace_s` — how long after a write to wait before checking (the inverter
    integration may need a poll cycle to read the register back).
    `fail_threshold` — consecutive mismatching checks before a mismatch is
    reported as persistent (→ repair issue).
    `reassert_interval_s` — minimum spacing between re-asserting one entity,
    so Solar AI never fights another controller in a tight loop.
    """

    grace_s: float = 45.0
    fail_threshold: int = 2
    reassert_interval_s: float = 600.0
    _expected: dict[str, _Expectation] = field(default_factory=dict)

    def expect(self, entity_id: str, value: Any, now: datetime) -> None:
        prev = self._expected.get(entity_id)
        exp = _Expectation(value, now)
        if prev is not None and values_match(prev.value, value):
            # Re-writing the same value keeps the mismatch history, so a
            # re-assert that doesn't stick still escalates.
            exp.mismatches = prev.mismatches
            exp.last_reassert = prev.last_reassert
        self._expected[entity_id] = exp

    def forget(self, entity_id: str) -> None:
        self._expected.pop(entity_id, None)

    def expected(self, entity_id: str) -> Any:
        exp = self._expected.get(entity_id)
        return exp.value if exp else None

    def evaluate(
        self, get_state: Callable[[str], Any], now: datetime,
    ) -> list[Mismatch]:
        out: list[Mismatch] = []
        for entity_id, exp in self._expected.items():
            if (now - exp.written_at).total_seconds() < self.grace_s:
                continue
            actual = get_state(entity_id)
            if actual in _UNAVAILABLE:
                continue  # can't judge — don't count against it
            if values_match(exp.value, actual):
                exp.mismatches = 0
                continue
            exp.mismatches += 1
            reassert = (
                exp.last_reassert is None
                or (now - exp.last_reassert).total_seconds() >= self.reassert_interval_s
            )
            if reassert:
                exp.last_reassert = now
            out.append(Mismatch(
                entity_id, exp.value, actual, exp.mismatches, reassert,
                exp.mismatches >= self.fail_threshold,
            ))
        return out
