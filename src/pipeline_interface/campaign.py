from __future__ import annotations

"""Offline campaign planning and waste-ledger accounting.

This module is intentionally JSON-friendly: all volumes are represented with
:class:`fractions.Fraction` internally and emitted as exact finite decimal
strings.  It never controls pumps or valves and never mutates its input.
"""

from dataclasses import dataclass, replace
from fractions import Fraction
from bisect import bisect_right
import re
from typing import Any


_DECIMAL_RE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")

START = "START"
SCHEDULED = "scheduled"
NOT_SELECTED = "not_selected"
COMPLIANT = "compliant"
BASELINE_CONFLICT = "baseline_conflict"
PLAN_INFEASIBLE = "new_plan_infeasible"
OPTIMAL = "optimal"


# ---------------------------------------------------------------------------
# Exact parsing and serialization
# ---------------------------------------------------------------------------


def _value_error(message: str) -> ValueError:
    return ValueError(message)


def parse_decimal_string(value: Any, name: str) -> Fraction:
    if not isinstance(value, str) or _DECIMAL_RE.fullmatch(value) is None:
        raise _value_error(f"{name} must be a decimal string")
    try:
        result = Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise _value_error(f"invalid {name}") from exc
    return result


def parse_exact_quantity(value: Any, name: str) -> Fraction:
    """Parse an exact environmental quantity.

    The campaign wire examples use decimal strings.  Integral JSON values are
    also accepted because they are exact; floats are refused so binary
    floating point can never decide a capacity boundary.
    """

    if isinstance(value, bool):
        raise _value_error(f"invalid {name}")
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, Fraction):
        return value
    return parse_decimal_string(value, name)


def require_positive_decimal(value: Any, name: str) -> Fraction:
    result = parse_decimal_string(value, name)
    if result <= 0:
        raise _value_error(f"{name} must be positive")
    return result


def require_nonnegative_decimal(value: Any, name: str) -> Fraction:
    result = parse_decimal_string(value, name)
    if result < 0:
        raise _value_error(f"{name} must be nonnegative")
    return result


def require_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _value_error(f"{name} must be an integer")
    return value


def require_nonempty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise _value_error(f"{name} must be a nonempty string")
    return value


def decimal_text(value: Fraction) -> str:
    """Format an exact finite decimal without binary float conversion."""

    if value.denominator == 1:
        return str(value.numerator)

    denominator = value.denominator
    twos = fives = 0
    rest = denominator
    while rest % 2 == 0:
        twos += 1
        rest //= 2
    while rest % 5 == 0:
        fives += 1
        rest //= 5
    if rest != 1:
        # Inputs are decimal strings, so this is only a defensive fallback.
        return value.numerator + "/" + value.denominator

    scale = max(twos, fives)
    scaled = abs(value.numerator) * (2 ** (scale - twos)) * (5 ** (scale - fives))
    digits = str(scaled).rjust(scale + 1, "0")
    if scale:
        whole = digits[:-scale]
        fraction = digits[-scale:].rstrip("0")
        text = whole + ("." + fraction if fraction else "")
    else:
        text = digits
    return ("-" if value.numerator < 0 else "") + text


def ceil_minutes(amount: Fraction, rate: Fraction) -> int:
    return -((-amount) // rate)


def decimal_scale_exponent(value: Fraction) -> int:
    denominator = value.denominator
    twos = fives = 0
    while denominator % 2 == 0:
        twos += 1
        denominator //= 2
    while denominator % 5 == 0:
        fives += 1
        denominator //= 5
    return max(twos, fives)


def scaled_units(value: Fraction, exponent: int) -> int:
    scale = 10 ** exponent
    return value.numerator * (scale // value.denominator)


def format_units(value: int, exponent: int) -> str:
    if exponent == 0:
        return str(value)
    sign = "-" if value < 0 else ""
    digits = str(abs(value)).rjust(exponent + 1, "0")
    whole = digits[:-exponent]
    fraction = digits[-exponent:].rstrip("0")
    return sign + whole + ("." + fraction if fraction else "")


# ---------------------------------------------------------------------------
# Validated input model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Batch:
    bid: str
    volume: Fraction
    release: int
    deadline: int
    priority: int
    duration: int


@dataclass(frozen=True, slots=True)
class Edge:
    flush_volume: Fraction
    flush_minutes: int
    duration: int
    raw_flush_volume: str
    flush_units: int = 0


@dataclass(frozen=True, slots=True)
class Drain:
    minute: int
    amount: Fraction
    amount_units: int = 0


@dataclass(frozen=True, slots=True)
class Inflow:
    event_id: str
    minute: int
    amount: Fraction
    source_ref: str
    amount_units: int = 0


@dataclass(frozen=True, slots=True)
class FixedEvent:
    minute: int
    drain: Drain | None
    inflows: tuple[Inflow, ...]
    inflow_total: Fraction
    drain_units: int = 0
    inflow_total_units: int = 0


@dataclass(frozen=True, slots=True)
class CampaignData:
    flow_rate: Fraction
    horizon: int
    batches: tuple[Batch, ...]  # sorted by ID
    id_to_index: dict[str, int]
    start_edges: tuple[Edge | None, ...]
    edges: tuple[tuple[Edge | None, ...], ...]
    initial_waste: Fraction
    waste_capacity: Fraction
    scale_exponent: int
    initial_units: int
    capacity_units: int
    drains: tuple[Drain, ...]
    inflows: tuple[Inflow, ...]
    fixed_events: tuple[FixedEvent, ...]
    fixed_minutes: tuple[int, ...]
    drain_minutes: tuple[int, ...]


def _validate_input(data: Any) -> CampaignData:
    if not isinstance(data, dict):
        raise ValueError("data must be an object")

    flow_rate = require_positive_decimal(data.get("flow_rate"), "flow_rate")
    horizon = require_int(data.get("horizon_minutes"), "horizon_minutes")
    if horizon <= 0:
        raise ValueError("horizon_minutes must be positive")

    raw_batches = data.get("batches")
    if not isinstance(raw_batches, list) or not 1 <= len(raw_batches) <= 18:
        raise ValueError("batches must contain 1..18 items")

    by_id: dict[str, Batch] = {}
    for pos, item in enumerate(raw_batches):
        if not isinstance(item, dict):
            raise ValueError(f"batches[{pos}] must be an object")
        bid = require_nonempty_string(item.get("id"), "batch id")
        if bid == START:
            raise ValueError("batch id cannot be reserved value START")
        if bid in by_id:
            raise ValueError(f"duplicate batch id: {bid}")
        volume = require_positive_decimal(item.get("volume"), f"batch {bid} volume")
        release = require_int(item.get("release_minute"), f"batch {bid} release_minute")
        deadline = require_int(item.get("deadline_minute"), f"batch {bid} deadline_minute")
        priority = require_int(item.get("priority"), f"batch {bid} priority")
        if release < 0:
            raise ValueError(f"batch {bid} release_minute must be nonnegative")
        if not (release < deadline <= horizon):
            raise ValueError(f"batch {bid} time window is invalid")
        if not 1 <= priority <= 5:
            raise ValueError(f"batch {bid} priority must be 1..5")
        by_id[bid] = Batch(
            bid=bid,
            volume=volume,
            release=release,
            deadline=deadline,
            priority=priority,
            duration=ceil_minutes(volume, flow_rate),
        )

    ids = tuple(sorted(by_id))
    n = len(ids)
    index = {bid: i for i, bid in enumerate(ids)}

    raw_transitions = data.get("transitions")
    if not isinstance(raw_transitions, list):
        raise ValueError("transitions must be a list")

    start_edges: list[Edge | None] = [None] * n
    edges: list[list[Edge | None]] = [[None] * n for _ in range(n)]
    seen_pairs: set[tuple[str, str]] = set()

    for pos, item in enumerate(raw_transitions):
        if not isinstance(item, dict):
            raise ValueError(f"transitions[{pos}] must be an object")
        source = require_nonempty_string(item.get("from"), f"transitions[{pos}].from")
        target = require_nonempty_string(item.get("to"), f"transitions[{pos}].to")
        if source != START and source not in index:
            raise ValueError(f"transition references unknown from batch: {source}")
        if target not in index:
            raise ValueError(f"transition references unknown to batch: {target}")
        if target == START:
            raise ValueError("START may only be a transition source")
        if source == target:
            raise ValueError("self transitions are not allowed")
        pair = (source, target)
        if pair in seen_pairs:
            raise ValueError(f"duplicate transition: {source}->{target}")
        seen_pairs.add(pair)

        flush_volume = require_nonnegative_decimal(
            item.get("flush_volume"), f"transition {source}->{target} flush_volume"
        )
        flush_minutes = require_int(
            item.get("flush_minutes"), f"transition {source}->{target} flush_minutes"
        )
        if flush_minutes < 0:
            raise ValueError("flush_minutes must be nonnegative")
        edge = Edge(
            flush_volume=flush_volume,
            flush_minutes=flush_minutes,
            duration=flush_minutes + ceil_minutes(flush_volume, flow_rate),
            raw_flush_volume=decimal_text(flush_volume),
        )
        j = index[target]
        if source == START:
            start_edges[j] = edge
        else:
            edges[index[source]][j] = edge

    initial_waste = parse_exact_quantity(data.get("initial_waste"), "initial_waste")
    if initial_waste < 0:
        raise ValueError("initial_waste must be nonnegative")
    waste_capacity = parse_exact_quantity(data.get("waste_capacity"), "waste_capacity")
    if waste_capacity <= 0:
        raise ValueError("waste_capacity must be positive")

    raw_drains = data.get("drains", [])
    raw_inflows = data.get("committed_inflows", [])
    if not isinstance(raw_drains, list) or not 0 <= len(raw_drains) <= 18:
        raise ValueError("drains must be a list containing 0..18 items")
    if not isinstance(raw_inflows, list) or not 0 <= len(raw_inflows) <= 18:
        raise ValueError("committed_inflows must be a list containing 0..18 items")

    drains: list[Drain] = []
    drain_minutes_seen: set[int] = set()
    for pos, item in enumerate(raw_drains):
        if not isinstance(item, dict):
            raise ValueError(f"drains[{pos}] must be an object")
        minute = require_int(item.get("minute"), f"drains[{pos}].minute")
        if not 0 <= minute <= horizon:
            raise ValueError("drain minute must be within 0..horizon_minutes")
        if minute in drain_minutes_seen:
            raise ValueError(f"duplicate drain minute: {minute}")
        drain_minutes_seen.add(minute)
        amount = parse_exact_quantity(item.get("amount"), f"drain at minute {minute} amount")
        if amount <= 0:
            raise ValueError("drain amount must be positive")
        drains.append(Drain(minute=minute, amount=amount))
    drains.sort(key=lambda d: d.minute)

    inflows: list[Inflow] = []
    event_ids: set[str] = set()
    for pos, item in enumerate(raw_inflows):
        if not isinstance(item, dict):
            raise ValueError(f"committed_inflows[{pos}] must be an object")
        event_id = require_nonempty_string(
            item.get("event_id"), f"committed_inflows[{pos}].event_id"
        )
        if event_id in event_ids:
            raise ValueError(f"duplicate committed event_id: {event_id}")
        event_ids.add(event_id)
        minute = require_int(item.get("minute"), f"committed_inflow {event_id} minute")
        if not 0 <= minute <= horizon:
            raise ValueError("committed inflow minute must be within 0..horizon_minutes")
        amount = parse_exact_quantity(item.get("amount"), f"committed_inflow {event_id} amount")
        if amount <= 0:
            raise ValueError("committed inflow amount must be positive")
        source_ref = require_nonempty_string(
            item.get("source_ref"), f"committed_inflow {event_id} source_ref"
        )
        inflows.append(
            Inflow(
                event_id=event_id,
                minute=minute,
                amount=amount,
                source_ref=source_ref,
            )
        )
    inflows.sort(key=lambda e: (e.minute, e.event_id))

    quantity_exponents = [
        decimal_scale_exponent(value)
        for value in (
            [flow_rate, initial_waste, waste_capacity]
            + [batch.volume for batch in by_id.values()]
            + [drain.amount for drain in drains]
            + [event.amount for event in inflows]
            + [
                edge.flush_volume
                for row in edges
                for edge in row
                if edge is not None
            ]
            + [edge.flush_volume for edge in start_edges if edge is not None]
        )
    ]
    scale_exponent = max(quantity_exponents, default=0)

    def units(value: Fraction) -> int:
        return scaled_units(value, scale_exponent)

    drains = [replace(drain, amount_units=units(drain.amount)) for drain in drains]
    inflows = [replace(event, amount_units=units(event.amount)) for event in inflows]
    start_edges = [
        None if edge is None else replace(edge, flush_units=units(edge.flush_volume))
        for edge in start_edges
    ]
    edges = [
        [
            None if edge is None else replace(edge, flush_units=units(edge.flush_volume))
            for edge in row
        ]
        for row in edges
    ]

    drain_by_minute = {d.minute: d for d in drains}
    inflow_by_minute: dict[int, list[Inflow]] = {}
    for event in inflows:
        inflow_by_minute.setdefault(event.minute, []).append(event)
    for minute_events in inflow_by_minute.values():
        minute_events.sort(key=lambda e: e.event_id)

    fixed_minutes = sorted(set(drain_by_minute) | set(inflow_by_minute))
    fixed_events = tuple(
        FixedEvent(
            minute=minute,
            drain=drain_by_minute.get(minute),
            inflows=tuple(inflow_by_minute.get(minute, ())),
            inflow_total=sum((e.amount for e in inflow_by_minute.get(minute, ())), Fraction(0)),
            drain_units=(
                drain_by_minute[minute].amount_units if minute in drain_by_minute else 0
            ),
            inflow_total_units=sum(
                event.amount_units for event in inflow_by_minute.get(minute, ())
            ),
        )
        for minute in fixed_minutes
    )

    return CampaignData(
        flow_rate=flow_rate,
        horizon=horizon,
        batches=tuple(by_id[bid] for bid in ids),
        id_to_index=index,
        start_edges=tuple(start_edges),
        edges=tuple(tuple(row) for row in edges),
        initial_waste=initial_waste,
        waste_capacity=waste_capacity,
        scale_exponent=scale_exponent,
        initial_units=units(initial_waste),
        capacity_units=units(waste_capacity),
        drains=tuple(drains),
        inflows=tuple(inflows),
        fixed_events=fixed_events,
        fixed_minutes=tuple(event.minute for event in fixed_events),
        drain_minutes=tuple(d.minute for d in drains),
    )


# ---------------------------------------------------------------------------
# Waste ledger
# ---------------------------------------------------------------------------


def _entry_basic(entry: dict[str, Any], entry_type: str, minute: int) -> dict[str, Any]:
    entry.update(
        {
            "minute": minute,
            "entry_type": entry_type,
            "event_type": entry_type,
            "kind": entry_type,
            "type": entry_type,
        }
    )
    return entry


def build_ledger(
    cd: CampaignData,
    flush_events: tuple[tuple[int, Fraction, str, str, int], ...] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Build the complete ledger.

    ``flush_events`` rows are ``(minute, volume, source, target, batch_index)``.
    The returned conflict is the first baseline/capacity conflict.  For a
    baseline conflict the ledger deliberately continues through every fixed
    event so no commitment is hidden.
    """

    capacity_text = decimal_text(cd.waste_capacity)
    balance = cd.initial_waste
    entries: list[dict[str, Any]] = [
        {
            "minute": 0,
            "entry_type": "initial_waste",
            "event_type": "initial_waste",
            "kind": "initial",
            "type": "initial",
            "phase": "before_minute_events",
            "amount": decimal_text(cd.initial_waste),
            "balance": decimal_text(balance),
            "waste_balance": decimal_text(balance),
            "balance_volume": decimal_text(balance),
            "waste_capacity": capacity_text,
            "capacity": capacity_text,
            "within_capacity": balance <= cd.waste_capacity,
        }
    ]

    first_conflict: dict[str, Any] | None = None
    if balance > cd.waste_capacity:
        first_conflict = {
            "code": "initial_waste_over_capacity",
            "environment_status": BASELINE_CONFLICT,
            "phase": "before_minute_events",
            "minute": 0,
            "balance": decimal_text(balance),
            "waste_capacity": capacity_text,
            "capacity": capacity_text,
            "message": "initial_waste already exceeds waste_capacity before minute 0",
        }

    flushes_by_minute: dict[int, list[tuple[int, Fraction, str, str, int]]] = {}
    for event in flush_events:
        flushes_by_minute.setdefault(event[0], []).append(event)
    for rows in flushes_by_minute.values():
        rows.sort(key=lambda row: row[4])  # batch order is also chronological

    minutes = sorted({event.minute for event in cd.fixed_events} | set(flushes_by_minute))
    fixed_by_minute = {event.minute: event for event in cd.fixed_events}

    for minute in minutes:
        fixed = fixed_by_minute.get(minute)

        # Mandatory order at every minute: drain -> committed -> new flush.
        if fixed is not None and fixed.drain is not None:
            requested = fixed.drain.amount
            removed = min(requested, balance)
            balance -= removed
            entries.append(
                _entry_basic(
                    {
                        "drain": True,
                        "amount": decimal_text(requested),
                        "requested_amount": decimal_text(requested),
                        "removed_amount": decimal_text(removed),
                        "balance": decimal_text(balance),
                        "waste_balance": decimal_text(balance),
                        "balance_volume": decimal_text(balance),
                    },
                    "drain",
                    minute,
                )
            )

        if fixed is not None:
            for inflow in fixed.inflows:
                balance += inflow.amount
                entries.append(
                    _entry_basic(
                        {
                            "event_id": inflow.event_id,
                            "source_ref": inflow.source_ref,
                            "amount": decimal_text(inflow.amount),
                            "balance": decimal_text(balance),
                            "waste_balance": decimal_text(balance),
                            "balance_volume": decimal_text(balance),
                        },
                        "committed_inflow",
                        minute,
                    )
                )

            if balance > cd.waste_capacity and first_conflict is None:
                first_conflict = {
                    "code": "committed_inflows_over_capacity",
                    "environment_status": BASELINE_CONFLICT,
                    "phase": "committed_inflows",
                    "minute": minute,
                    "event_ids": [event.event_id for event in fixed.inflows],
                    "balance": decimal_text(balance),
                    "waste_capacity": capacity_text,
                    "capacity": capacity_text,
                    "message": "committed inflows exceed waste_capacity after drain",
                }

        for transition_order, (_, volume, source, target, batch_index) in enumerate(
            flushes_by_minute.get(minute, ()), start=1
        ):
            balance += volume
            over = balance > cd.waste_capacity
            entries.append(
                _entry_basic(
                    {
                        "from": source,
                        "to": target,
                        "batch_id": cd.batches[batch_index].bid,
                        "transition_to": target,
                        "transition_from": source,
                        "transition_order": transition_order,
                        "amount": decimal_text(volume),
                        "flush_volume": decimal_text(volume),
                        "balance": decimal_text(balance),
                        "waste_balance": decimal_text(balance),
                        "balance_volume": decimal_text(balance),
                        "within_capacity": not over,
                    },
                    "campaign_transition_inflow",
                    minute,
                )
            )
            if over and first_conflict is None:
                # During planning this is treated as plan infeasibility and the
                # candidate timing is rejected before it becomes a result.
                first_conflict = {
                    "code": "campaign_transition_over_capacity",
                    "environment_status": COMPLIANT,
                    "plan_status": PLAN_INFEASIBLE,
                    "phase": "campaign_transition",
                    "minute": minute,
                    "from": source,
                    "to": target,
                    "batch_id": cd.batches[batch_index].bid,
                    "balance": decimal_text(balance),
                    "waste_capacity": capacity_text,
                    "capacity": capacity_text,
                }

    for entry in entries:
        if "balance" in entry:
            entry.setdefault("balance_after", entry["balance"])
            entry.setdefault("running_balance", entry["balance"])

    return entries, first_conflict


def balance_after(
    cd: CampaignData,
    target_minute: int,
    flushes: tuple[tuple[int, Fraction], ...] = (),
) -> Fraction:
    """Balance after all fixed and prefix flush events <= target_minute."""

    balance = cd.initial_waste
    fi = qi = 0
    fixed = cd.fixed_events
    while fi < len(fixed) or qi < len(flushes):
        fixed_minute = fixed[fi].minute if fi < len(fixed) else None
        flush_minute = flushes[qi][0] if qi < len(flushes) else None
        if fixed_minute is not None and flush_minute is not None:
            minute = min(fixed_minute, flush_minute)
        elif fixed_minute is not None:
            minute = fixed_minute
        else:
            minute = flush_minute  # type: ignore[assignment]

        if minute > target_minute:
            break

        if fixed_minute == minute:
            event = fixed[fi]
            if event.drain is not None:
                balance -= min(event.drain.amount, balance)
            balance += event.inflow_total
            fi += 1
        while qi < len(flushes) and flushes[qi][0] == minute:
            balance += flushes[qi][1]
            qi += 1
    return balance


def earliest_flush_minute(
    cd: CampaignData,
    flushes: tuple[tuple[int, Fraction], ...],
    flush_volume: Fraction,
    lower_bound: int,
    latest_minute: int,
) -> int | None:
    if lower_bound > latest_minute:
        return None

    def feasible(minute: int) -> bool:
        balance = balance_after(cd, minute, flushes)
        return balance + flush_volume <= cd.waste_capacity

    if feasible(lower_bound):
        return lower_bound

    # A balance can only improve at a drain.  Committed inflows and new flushes
    # increase it, so scanning the <=18 drain minutes is sufficient; there is
    # no need to iterate every minute of a potentially huge horizon.
    for minute in cd.drain_minutes:
        if minute <= lower_bound:
            continue
        if minute > latest_minute:
            break
        if feasible(minute):
            return minute
    return None


# ---------------------------------------------------------------------------
# Pareto labels and subset DP
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Label:
    mask: int
    last: int
    end_minute: int
    flush_units: int
    completion_sum: int
    lex_code: int
    balance_end: int
    parent: "Label | None" = None


def apply_fixed_units(balance: int, event: FixedEvent) -> int:
    if event.drain is not None:
        balance -= min(event.drain_units, balance)
    return balance + event.inflow_total_units


def advance_fixed_units(
    cd: CampaignData, balance: int, event_index: int, target_minute: int
) -> tuple[int, int]:
    events = cd.fixed_events
    while event_index < len(events) and events[event_index].minute <= target_minute:
        balance = apply_fixed_units(balance, events[event_index])
        event_index += 1
    return balance, event_index


def find_transition_end(
    cd: CampaignData,
    previous_end: int,
    previous_balance: int,
    duration: int,
    flush_units: int,
    release_minute: int,
    latest_minute: int,
    ledger_state_minute: int | None = None,
) -> tuple[int, int, int] | None:
    """Return earliest feasible transition end and ledger state at that minute.

    ``previous_end`` is the ready time used for scheduling.  For the START
    transition the ledger state is immediately before minute-zero processing,
    represented by ``ledger_state_minute=-1``.  The returned event index points
    just past fixed events at the end minute.
    """

    lower = max(previous_end + duration, release_minute)
    if lower > latest_minute:
        return None

    if ledger_state_minute is None:
        ledger_state_minute = previous_end
    event_index = bisect_right(cd.fixed_minutes, ledger_state_minute)
    balance, event_index = advance_fixed_units(cd, previous_balance, event_index, lower)
    if balance + flush_units <= cd.capacity_units:
        return lower, balance, event_index

    # After lower, only a drain can reduce the balance; scan those boundaries
    # rather than every integer minute.
    for drain_minute in cd.drain_minutes:
        if drain_minute <= lower:
            continue
        if drain_minute > latest_minute:
            break
        balance, event_index = advance_fixed_units(
            cd, balance, event_index, drain_minute
        )
        if balance + flush_units <= cd.capacity_units:
            return drain_minute, balance, event_index
    return None


def objective_dominates(
    flush_a: int,
    completion_a: int,
    lex_a: int,
    flush_b: int,
    completion_b: int,
    lex_b: int,
) -> bool:
    if flush_a < flush_b:
        return True
    if flush_a > flush_b:
        return False
    if completion_a < completion_b:
        return True
    if completion_a > completion_b:
        return False
    return lex_a <= lex_b


def state_dominates(
    cd: CampaignData,
    end_a: int,
    balance_a: int,
    end_b: int,
    balance_b: int,
) -> bool:
    if end_a > end_b:
        return False

    event_index = bisect_right(cd.fixed_minutes, end_a)
    balance_a, event_index = advance_fixed_units(cd, balance_a, event_index, end_b)
    if balance_a > balance_b:
        return False

    future_index = bisect_right(cd.fixed_minutes, end_b)
    balance_b_future = balance_b
    balance_a_future = balance_a
    for event in cd.fixed_events[future_index:]:
        balance_a_future = apply_fixed_units(balance_a_future, event)
        balance_b_future = apply_fixed_units(balance_b_future, event)
        if balance_a_future > balance_b_future:
            return False
    return True


def insert_values(
    cd: CampaignData,
    frontier: list[Label],
    *,
    mask: int,
    last: int,
    end_minute: int,
    flush_units: int,
    completion_sum: int,
    lex_code: int,
    balance_end: int,
    parent: Label | None,
) -> None:
    # Most generated states are rejected here.  Keeping this allocation-free
    # until a candidate is accepted makes the 18-batch DP practical.
    has_fixed_events = bool(cd.fixed_events)
    for existing in frontier:
        if existing.end_minute > end_minute:
            continue
        if existing.flush_units < flush_units:
            objective_better = True
        elif existing.flush_units == flush_units:
            objective_better = (
                existing.completion_sum < completion_sum
                or (
                    existing.completion_sum == completion_sum
                    and existing.lex_code <= lex_code
                )
            )
        else:
            objective_better = False
        if not objective_better:
            continue
        if not has_fixed_events or state_dominates(
            cd,
            existing.end_minute,
            existing.balance_end,
            end_minute,
            balance_end,
        ):
            return

    kept: list[Label] = []
    for existing in frontier:
        if end_minute > existing.end_minute:
            candidate_objective = False
        elif flush_units < existing.flush_units:
            candidate_objective = True
        elif flush_units == existing.flush_units:
            candidate_objective = (
                completion_sum < existing.completion_sum
                or (
                    completion_sum == existing.completion_sum
                    and lex_code <= existing.lex_code
                )
            )
        else:
            candidate_objective = False

        candidate_dominates = candidate_objective and (
            not has_fixed_events
            or state_dominates(
                cd,
                end_minute,
                balance_end,
                existing.end_minute,
                existing.balance_end,
            )
        )
        if not candidate_dominates:
            kept.append(existing)

    kept.append(
        Label(
            mask=mask,
            last=last,
            end_minute=end_minute,
            flush_units=flush_units,
            completion_sum=completion_sum,
            lex_code=lex_code,
            balance_end=balance_end,
            parent=parent,
        )
    )
    frontier[:] = kept


def solve_campaign(cd: CampaignData) -> tuple[Label | None, set[int]]:
    n = len(cd.batches)
    size = 1 << n
    dp: list[list[Label] | None] = [None] * (size * n)
    committable_flags = [False] * n

    # START -> first batch labels.
    for j, batch in enumerate(cd.batches):
        edge = cd.start_edges[j]
        if edge is None:
            continue
        latest = batch.deadline - batch.duration
        if not cd.fixed_events:
            end_transition = max(edge.duration, batch.release)
            if end_transition > latest:
                continue
            balance_end = cd.initial_units + edge.flush_units
            if balance_end > cd.capacity_units:
                continue
        else:
            timing = find_transition_end(
                cd,
                0,
                cd.initial_units,
                edge.duration,
                edge.flush_units,
                batch.release,
                latest,
                ledger_state_minute=-1,
            )
            if timing is None:
                continue
            end_transition, balance_at_transition, event_index = timing
            completion_preview = end_transition + batch.duration
            balance_after_flush = balance_at_transition + edge.flush_units
            balance_end, _ = advance_fixed_units(
                cd, balance_after_flush, event_index, completion_preview
            )
        committable_flags[j] = True
        completion = end_transition + batch.duration
        mask = 1 << j
        slot = mask * n + j
        frontier = dp[slot]
        if frontier is None:
            frontier = []
            dp[slot] = frontier
        insert_values(
            cd,
            frontier,
            mask=mask,
            last=j,
            end_minute=completion,
            flush_units=edge.flush_units,
            completion_sum=completion,
            lex_code=j,
            balance_end=balance_end,
            parent=None,
        )

    full_mask = size - 1
    for mask in range(1, size):
        remaining = full_mask ^ mask
        if remaining == 0:
            continue
        bits = mask
        while bits:
            low = bits & -bits
            i = low.bit_length() - 1
            bits -= low
            frontier = dp[mask * n + i]
            if not frontier:
                continue
            candidates_bits = remaining
            while candidates_bits:
                low_j = candidates_bits & -candidates_bits
                j = low_j.bit_length() - 1
                candidates_bits -= low_j
                edge = cd.edges[i][j]
                if edge is None:
                    continue
                batch = cd.batches[j]
                new_mask = mask | low_j
                slot = new_mask * n + j
                target_frontier = dp[slot]
                if target_frontier is None:
                    target_frontier = []
                    dp[slot] = target_frontier

                latest = batch.deadline - batch.duration
                edge_duration = edge.duration
                edge_flush_units = edge.flush_units
                batch_release = batch.release
                batch_duration = batch.duration

                for old in frontier:
                    if not cd.fixed_events:
                        end_transition = max(
                            old.end_minute + edge_duration, batch_release
                        )
                        if end_transition > latest:
                            continue
                        balance_end = old.balance_end + edge_flush_units
                        if balance_end > cd.capacity_units:
                            continue
                    else:
                        timing = find_transition_end(
                            cd,
                            old.end_minute,
                            old.balance_end,
                            edge_duration,
                            edge_flush_units,
                            batch_release,
                            latest,
                        )
                        if timing is None:
                            continue
                        end_transition, balance_at_transition, event_index = timing
                        completion_preview = end_transition + batch_duration
                        balance_after_flush = (
                            balance_at_transition + edge_flush_units
                        )
                        balance_end, _ = advance_fixed_units(
                            cd,
                            balance_after_flush,
                            event_index,
                            completion_preview,
                        )

                    committable_flags[j] = True
                    completion = end_transition + batch_duration
                    candidate_flush = old.flush_units + edge_flush_units
                    candidate_lex = (old.lex_code << 5) | j

                    # Fast path: almost every target frontier has one label in
                    # dense regular instances.  Avoid a function call and the
                    # general list scan for the common rejection.
                    if not target_frontier:
                        target_frontier.append(
                            Label(
                                mask=new_mask,
                                last=j,
                                end_minute=completion,
                                flush_units=candidate_flush,
                                completion_sum=old.completion_sum + completion,
                                lex_code=candidate_lex,
                                balance_end=balance_end,
                                parent=old,
                            )
                        )
                    elif len(target_frontier) == 1:
                        existing = target_frontier[0]
                        existing_objective = (
                            existing.flush_units < candidate_flush
                            or (
                                existing.flush_units == candidate_flush
                                and (
                                    existing.completion_sum
                                    < old.completion_sum + completion
                                    or (
                                        existing.completion_sum
                                        == old.completion_sum + completion
                                        and existing.lex_code <= candidate_lex
                                    )
                                )
                            )
                        )
                        existing_environment = (
                            not cd.fixed_events
                            or state_dominates(
                                cd,
                                existing.end_minute,
                                existing.balance_end,
                                completion,
                                balance_end,
                            )
                        )
                        if (
                            existing.end_minute <= completion
                            and existing_objective
                            and existing_environment
                        ):
                            continue

                        candidate_completion_sum = old.completion_sum + completion
                        candidate_objective = (
                            candidate_flush < existing.flush_units
                            or (
                                candidate_flush == existing.flush_units
                                and (
                                    candidate_completion_sum
                                    < existing.completion_sum
                                    or (
                                        candidate_completion_sum
                                        == existing.completion_sum
                                        and candidate_lex <= existing.lex_code
                                    )
                                )
                            )
                        )
                        candidate_environment = (
                            not cd.fixed_events
                            or state_dominates(
                                cd,
                                completion,
                                balance_end,
                                existing.end_minute,
                                existing.balance_end,
                            )
                        )
                        if (
                            completion <= existing.end_minute
                            and candidate_objective
                            and candidate_environment
                        ):
                            target_frontier[0] = Label(
                                mask=new_mask,
                                last=j,
                                end_minute=completion,
                                flush_units=candidate_flush,
                                completion_sum=candidate_completion_sum,
                                lex_code=candidate_lex,
                                balance_end=balance_end,
                                parent=old,
                            )
                        else:
                            target_frontier.append(
                                Label(
                                    mask=new_mask,
                                    last=j,
                                    end_minute=completion,
                                    flush_units=candidate_flush,
                                    completion_sum=candidate_completion_sum,
                                    lex_code=candidate_lex,
                                    balance_end=balance_end,
                                    parent=old,
                                )
                            )
                    else:
                        insert_values(
                            cd,
                            target_frontier,
                            mask=new_mask,
                            last=j,
                            end_minute=completion,
                            flush_units=candidate_flush,
                            completion_sum=old.completion_sum + completion,
                            lex_code=candidate_lex,
                            balance_end=balance_end,
                            parent=old,
                        )

    priority_masks = tuple(
        sum(1 << i for i, batch in enumerate(cd.batches) if batch.priority == p)
        for p in range(1, 6)
    )

    def priority_vector(mask: int) -> tuple[int, ...]:
        return tuple((mask & priority_masks[p - 1]).bit_count() for p in range(1, 6))

    empty = Label(
        mask=0,
        last=-1,
        end_minute=0,
        flush_units=0,
        completion_sum=0,
        lex_code=0,
        balance_end=cd.initial_units,
    )
    best: Label = empty
    best_vector = (0, 0, 0, 0, 0)
    for mask in range(1, size):
        vector = priority_vector(mask)
        for i in range(n):
            frontier = dp[mask * n + i]
            if not frontier:
                continue
            for label in frontier:
                if vector > best_vector:
                    best = label
                    best_vector = vector
                elif vector == best_vector:
                    if label.flush_units < best.flush_units or (
                        label.flush_units == best.flush_units
                        and (
                            label.completion_sum < best.completion_sum
                            or (
                                label.completion_sum == best.completion_sum
                                and label.lex_code < best.lex_code
                            )
                        )
                    ):
                        best = label
    committable = {i for i, value in enumerate(committable_flags) if value}
    return (None if best.mask == 0 else best), committable


# ---------------------------------------------------------------------------
# Result assembly
# ---------------------------------------------------------------------------


def canonical_environment(cd: CampaignData) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    drains = [
        {"minute": drain.minute, "amount": decimal_text(drain.amount)} for drain in cd.drains
    ]
    inflows = [
        {
            "event_id": event.event_id,
            "minute": event.minute,
            "amount": decimal_text(event.amount),
            "source_ref": event.source_ref,
        }
        for event in cd.inflows
    ]
    return drains, inflows


class PlanView(list):
    """Executable plan rows with mapping-style metadata compatibility.

    The primary executable content is a list of scheduled batch rows.  The
    same object also exposes the documented plan metadata by string key, so
    both ``result["plan"][0]`` and ``result["plan"]["sequence"]`` are useful.
    """

    def __init__(self, rows: list[dict[str, Any]], metadata: dict[str, Any]):
        super().__init__(rows)
        self.metadata = metadata

    def __getitem__(self, key: int | slice | str):  # type: ignore[override]
        if isinstance(key, str):
            return self.metadata[key]
        return list.__getitem__(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        if key in self.metadata:
            return self.metadata[key]
        if key == "batches":
            return list(self)
        return default

    def __contains__(self, key: object) -> bool:
        if isinstance(key, str):
            return key in self.metadata
        return list.__contains__(self, key)

    def keys(self):
        return self.metadata.keys()

    def values(self):
        return self.metadata.values()

    def items(self):
        return self.metadata.items()


def empty_plan(reason: str | None = None) -> dict[str, Any]:
    status = reason or "empty"
    return {
        "status": status,
        "plan_status": status,
        "is_empty": True,
        "sequence": [],
        "selected": [],
        "selected_ids": [],
        "selected_batch_ids": [],
        "batches": [],
        "transitions": [],
        "priority_counts": [0, 0, 0, 0, 0],
        "priority_counts_by_level": {str(p): 0 for p in range(1, 6)},
        "batch_count": 0,
        "total_flush_volume": "0",
        "flush_volume_total": "0",
        "total_completion_minutes": 0,
        "completion_minutes_sum": 0,
        "reason": reason,
    }


def baseline_conflict_result(cd: CampaignData) -> dict[str, Any]:
    ledger, conflict = build_ledger(cd)
    drains, inflows = canonical_environment(cd)
    plan = empty_plan(BASELINE_CONFLICT)
    batch_rows = [
        {
            "id": batch.bid,
            "batch_id": batch.bid,
            "status": NOT_SELECTED,
            "selected": False,
            "start_minute": None,
            "start": None,
            "complete_minute": None,
            "completion_minute": None,
            "end_minute": None,
            "finish_minute": None,
            "preceding_transition": None,
            "transition": None,
            "reason": BASELINE_CONFLICT,
            "release_minute": batch.release,
            "deadline_minute": batch.deadline,
            "priority": batch.priority,
            "volume": decimal_text(batch.volume),
        }
        for batch in cd.batches
    ]
    plan["all_batches"] = batch_rows
    plan_view = PlanView([], plan)
    baseline = {
        "status": BASELINE_CONFLICT,
        "environment_status": BASELINE_CONFLICT,
        "initial_waste": decimal_text(cd.initial_waste),
        "waste_capacity": decimal_text(cd.waste_capacity),
        "first_conflict": conflict,
    }
    return {
        "status": BASELINE_CONFLICT,
        "campaign_status": BASELINE_CONFLICT,
        "result_status": BASELINE_CONFLICT,
        "ok": False,
        "feasible": False,
        "environment_status": BASELINE_CONFLICT,
        "plan_status": BASELINE_CONFLICT,
        "planning_status": BASELINE_CONFLICT,
        "new_plan_infeasible": False,
        "first_conflict": conflict,
        "first_conflict_minute": conflict["minute"] if conflict else None,
        "conflict": conflict,
        "baseline": baseline,
        "plan": plan_view,
        "new_plan": plan_view,
        "campaign": plan_view,
        "campaign_plan": plan_view,
        "schedule": plan_view,
        "plan_detail": plan,
        "plan_object": plan,
        "plan_dict": plan,
        "sequence": [],
        "selected": [],
        "selected_ids": [],
        "selected_batch_ids": [],
        "selected_batches": [],
        "planned_batches": [],
        "selected_batch_details": [],
        "plan_batches": [],
        "new_plan_batches": [],
        "not_selected_batches": batch_rows,
        "excluded_batches": batch_rows,
        "batches": batch_rows,
        "candidates": batch_rows,
        "waste_ledger": ledger,
        "ledger": ledger,
        "drains": drains,
        "fixed_drains": drains,
        "committed_inflows": inflows,
        "existing_inflows": inflows,
        "initial_waste": decimal_text(cd.initial_waste),
        "waste_capacity": decimal_text(cd.waste_capacity),
        "horizon_minutes": cd.horizon,
    }


def reconstruct_plan(
    cd: CampaignData, best: Label | None
) -> tuple[dict[str, Any], list[dict[str, Any]], tuple[tuple[int, Fraction, str, str, int], ...], set[int]]:
    if best is None:
        plan = empty_plan(PLAN_INFEASIBLE)
        return plan, [], (), set()

    scheduled_rows: list[dict[str, Any]] = []
    transition_rows: list[dict[str, Any]] = []
    flush_events: list[tuple[int, Fraction, str, str, int]] = []
    path_reversed: list[int] = []
    cursor: Label | None = best
    while cursor is not None and cursor.last >= 0:
        path_reversed.append(cursor.last)
        cursor = cursor.parent
    path = tuple(reversed(path_reversed))
    selected: set[int] = set(path)
    ready_minute = 0

    for order, j in enumerate(path, start=1):
        batch = cd.batches[j]
        if order == 1:
            source = START
            edge = cd.start_edges[j]
        else:
            previous = path[order - 2]
            source = cd.batches[previous].bid
            edge = cd.edges[previous][j]
        assert edge is not None  # DP only created labels over existing edges

        lower = max(ready_minute + edge.duration, batch.release)
        end_transition = earliest_flush_minute(
            cd,
            tuple((minute, volume) for minute, volume, *_ in flush_events),
            edge.flush_volume,
            lower,
            batch.deadline - batch.duration,
        )
        assert end_transition is not None
        start_transition = end_transition - edge.duration
        waiting_minutes = start_transition - ready_minute
        completion = end_transition + batch.duration
        target = batch.bid

        transition = {
            "order": order,
            "sequence_index": order - 1,
            "from": source,
            "to": target,
            "batch_id": target,
            "start_minute": start_transition,
            "start": start_transition,
            "end_minute": end_transition,
            "end": end_transition,
            "finish_minute": end_transition,
            "duration_minutes": edge.duration,
            "flush_minutes": edge.flush_minutes,
            "flush_volume": edge.raw_flush_volume,
            "amount": edge.raw_flush_volume,
            "volume": edge.raw_flush_volume,
            "waiting_minutes": waiting_minutes,
        }
        transition_rows.append(transition)
        flush_events.append(
            (end_transition, edge.flush_volume, source, target, j)
        )

        row = {
            "order": order,
            "sequence_index": order - 1,
            "id": target,
            "batch_id": target,
            "status": SCHEDULED,
            "selected": True,
            "start_minute": end_transition,
            "start": end_transition,
            "started_at_minute": end_transition,
            "complete_minute": completion,
            "completion": completion,
            "completed_minute": completion,
            "completion_minute": completion,
            "end_minute": completion,
            "finish_minute": completion,
            "duration_minutes": batch.duration,
            "volume": decimal_text(batch.volume),
            "release_minute": batch.release,
            "deadline_minute": batch.deadline,
            "priority": batch.priority,
            "preceding_transition": transition,
            "transition": transition,
            "flush_volume": edge.raw_flush_volume,
        }
        scheduled_rows.append(row)
        ready_minute = completion

    counts = [0, 0, 0, 0, 0]
    for j in path:
        counts[cd.batches[j].priority - 1] += 1

    total_flush_text = format_units(best.flush_units, cd.scale_exponent)
    plan = {
        "status": OPTIMAL,
        "plan_status": OPTIMAL,
        "is_empty": False,
        "sequence": [cd.batches[j].bid for j in path],
        "selected": [cd.batches[j].bid for j in path],
        "selected_ids": [cd.batches[j].bid for j in path],
        "selected_batch_ids": [cd.batches[j].bid for j in path],
        "batches": scheduled_rows,
        "transitions": transition_rows,
        "priority_counts": counts,
        "priority_counts_by_level": {str(p): counts[p - 1] for p in range(1, 6)},
        "batch_count": len(path),
        "total_flush_volume": total_flush_text,
        "flush_volume_total": total_flush_text,
        "total_completion_minutes": best.completion_sum,
        "completion_minutes_sum": best.completion_sum,
        "end_minute": best.end_minute,
        "makespan_minutes": best.end_minute,
        "reason": None,
    }
    return plan, scheduled_rows, tuple(flush_events), selected


def plan_campaign(data: dict[str, Any]) -> dict[str, Any]:
    """Plan committable campaign batches and return the complete waste ledger.

    Invalid structures, identities, references, quantities or times raise
    :class:`ValueError`.  A baseline environmental conflict or an infeasible
    candidate plan is returned as a normal result.
    """

    cd = _validate_input(data)

    baseline_ledger, baseline_conflict = build_ledger(cd)
    if baseline_conflict is not None and baseline_conflict["code"] in {
        "initial_waste_over_capacity",
        "committed_inflows_over_capacity",
    }:
        return baseline_conflict_result(cd)

    best, committable = solve_campaign(cd)
    plan, scheduled_rows, flush_events, selected_indices = reconstruct_plan(cd, best)
    ledger, final_conflict = build_ledger(cd, flush_events)
    # Planning rejects every timing that would create this conflict, so a
    # conflict here would indicate an implementation bug rather than a normal
    # result state.
    if final_conflict is not None and final_conflict["code"] == "campaign_transition_over_capacity":
        raise RuntimeError("internal error: selected campaign exceeds waste capacity")

    drains, inflows = canonical_environment(cd)

    batch_rows: list[dict[str, Any]] = []
    scheduled_by_id = {row["batch_id"]: row for row in scheduled_rows}
    for j, batch in enumerate(cd.batches):
        if batch.bid in scheduled_by_id:
            batch_rows.append(scheduled_by_id[batch.bid])
            continue
        can_commit = j in committable
        row: dict[str, Any] = {
            "id": batch.bid,
            "batch_id": batch.bid,
            "status": NOT_SELECTED,
            "selected": False,
            "start_minute": None,
            "start": None,
            "complete_minute": None,
            "completion_minute": None,
            "end_minute": None,
            "finish_minute": None,
            "preceding_transition": None,
            "transition": None,
            "release_minute": batch.release,
            "deadline_minute": batch.deadline,
            "priority": batch.priority,
            "volume": decimal_text(batch.volume),
            "committable": can_commit,
        }
        if not can_commit:
            row["reason"] = PLAN_INFEASIBLE
            row["infeasibility_reason"] = PLAN_INFEASIBLE
        else:
            row["reason"] = NOT_SELECTED
        batch_rows.append(row)

    if best is None:
        status = PLAN_INFEASIBLE
        ok = feasible = False
        plan_status = PLAN_INFEASIBLE
        plan["all_batches"] = batch_rows
    else:
        status = "feasible"
        ok = feasible = True
        plan_status = OPTIMAL
        plan["all_batches"] = batch_rows
    plan_view = PlanView(scheduled_rows, plan)

    baseline = {
        "status": COMPLIANT,
        "environment_status": COMPLIANT,
        "initial_waste": decimal_text(cd.initial_waste),
        "waste_capacity": decimal_text(cd.waste_capacity),
        "first_conflict": None,
        "ledger_without_campaign": baseline_ledger,
    }

    result: dict[str, Any] = {
        "status": status,
        "campaign_status": status,
        "result_status": status,
        "ok": ok,
        "feasible": feasible,
        "environment_status": COMPLIANT,
        "plan_status": plan_status,
        "planning_status": plan_status,
        "first_conflict": None,
        "first_conflict_minute": None,
        "conflict": None,
        "new_plan_infeasible": best is None,
        "baseline": baseline,
        "plan": plan_view,
        "new_plan": plan_view,
        "campaign": plan_view,
        "campaign_plan": plan_view,
        "schedule": plan_view,
        "plan_detail": plan,
        "plan_object": plan,
        "plan_dict": plan,
        "sequence": plan["sequence"],
        "selected": plan["selected_batch_ids"],
        "selected_ids": plan["selected_batch_ids"],
        "selected_batch_ids": plan["selected_batch_ids"],
        "selected_batches": scheduled_rows,
        "planned_batches": scheduled_rows,
        "selected_batch_details": scheduled_rows,
        "plan_batches": scheduled_rows,
        "new_plan_batches": scheduled_rows,
        "not_selected_batches": [row for row in batch_rows if not row["selected"]],
        "excluded_batches": [row for row in batch_rows if not row["selected"]],
        "total_flush_volume": plan["total_flush_volume"],
        "total_completion_minutes": plan["total_completion_minutes"],
        "priority_counts": plan["priority_counts"],
        "batches": batch_rows,
        "candidates": batch_rows,
        "waste_ledger": ledger,
        "ledger": ledger,
        "drains": drains,
        "fixed_drains": drains,
        "committed_inflows": inflows,
        "existing_inflows": inflows,
        "initial_waste": decimal_text(cd.initial_waste),
        "waste_capacity": decimal_text(cd.waste_capacity),
        "horizon_minutes": cd.horizon,
    }
    return result
