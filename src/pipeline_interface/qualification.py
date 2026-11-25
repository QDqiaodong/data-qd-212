from __future__ import annotations

from bisect import bisect_left, bisect_right
from fractions import Fraction
import re
from typing import Any

from .service import _text, process


_DECIMAL_RE = re.compile(
    r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z"
)

NONDECREASING = "nondecreasing"
NONINCREASING = "nonincreasing"


def _decimal(value: Any, name: str) -> Fraction:
    """Parse a required exact decimal string."""
    if not isinstance(value, str) or _DECIMAL_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a decimal string")
    try:
        return Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"invalid {name}") from exc


def _exact_amount(value: Any, name: str, *, positive: bool = False) -> Fraction:
    """Parse an exact quantity.

    The wire contract uses decimal strings.  Accepting integral Python values
    as well is harmless because they are exact; floats are deliberately refused
    so that no boundary or status is decided by binary floating point.
    """
    if isinstance(value, bool):
        raise ValueError(f"invalid {name}")
    if isinstance(value, int):
        result = Fraction(value)
    elif isinstance(value, Fraction):
        result = value
    else:
        result = _decimal(value, name)

    if result < 0 or (positive and result == 0):
        raise ValueError(f"invalid {name}")
    return result


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _interval_json(start: Fraction, end: Fraction) -> dict[str, str]:
    start_text = _text(start)
    end_text = _text(end)
    volume_text = _text(end - start)
    return {
        "start": start_text,
        "end": end_text,
        "left": start_text,
        "right": end_text,
        "from": start_text,
        "to": end_text,
        "volume": volume_text,
        "length": volume_text,
    }


def qualify(data: dict) -> dict:
    """Qualify terminal deliveries and atomically allocate withdrawals.

    The accounting result of :func:`pipeline_interface.process` is retained and
    batch rows are augmented with qualification information.  All arithmetic
    uses :class:`fractions.Fraction`.
    """
    if not isinstance(data, dict):
        raise ValueError("data must be an object")

    # Preserve the existing accounting contract exactly.
    result = process(data)
    terminal = Fraction(result["terminal_volume"])
    batch_rows = result["batches"]
    batch_ids = [row["batch_id"] for row in batch_rows]
    batch_id_set = set(batch_ids)

    raw_specs = data.get("specs")
    if isinstance(raw_specs, dict):
        spec_entries = []
        for key, value in raw_specs.items():
            if not isinstance(value, dict):
                raise ValueError("each spec must be an object")
            entry = dict(value)
            entry.setdefault("batch_id", key)
            spec_entries.append(entry)
    elif isinstance(raw_specs, list):
        spec_entries = raw_specs
    else:
        raise ValueError("specs must be a list or mapping")

    specs: dict[str, dict[str, Fraction]] = {}
    for spec in spec_entries:
        if not isinstance(spec, dict):
            raise ValueError("each spec must be an object")
        batch_id = spec.get("batch_id")
        if batch_id is None:
            batch_id = spec.get("id")
        if batch_id is None:
            batch_id = spec.get("batch")
        if not _is_nonempty_str(batch_id):
            raise ValueError("spec batch_id must be a non-empty string")
        if batch_id in specs:
            raise ValueError(f"duplicate spec for batch {batch_id}")

        lower = _decimal(spec.get("min_component"), "min_component")
        upper = _decimal(spec.get("max_component"), "max_component")
        if not (0 <= lower <= 1 and 0 <= upper <= 1) or lower > upper:
            raise ValueError("component specification bounds are invalid")
        max_gap = _exact_amount(spec.get("max_gap"), "max_gap", positive=True)

        specs[batch_id] = {
            "min_component": lower,
            "max_component": upper,
            "max_gap": max_gap,
        }

    if set(specs) != batch_id_set or len(specs) != len(batch_ids):
        raise ValueError("specs must cover every batch exactly once")

    raw_samples = data.get("samples")
    if not isinstance(raw_samples, list) or not 1 <= len(raw_samples) <= 20000:
        raise ValueError("samples must contain 1..20000 items")

    # Fraction has structural equality/hashing by rational value, so "60.0",
    # "60.00" and equivalent decimal spellings collapse to one position.
    groups: dict[Fraction, list[tuple[str, Fraction, Fraction]]] = {}
    seen_sample_ids: set[str] = set()
    for sample in raw_samples:
        if not isinstance(sample, dict):
            raise ValueError("each sample must be an object")

        sample_id = sample.get("sample_id")
        if not _is_nonempty_str(sample_id):
            raise ValueError("sample_id must be a non-empty string")
        if sample_id in seen_sample_ids:
            raise ValueError(f"duplicate sample_id {sample_id}")
        seen_sample_ids.add(sample_id)

        position = _decimal(sample.get("position"), "position")
        if not 0 <= position <= terminal:
            raise ValueError("sample position must lie within [0, terminal_volume]")

        component = _decimal(sample.get("component"), "component")
        uncertainty = _decimal(sample.get("uncertainty"), "uncertainty")
        if uncertainty < 0:
            raise ValueError("uncertainty must be non-negative")

        low = component - uncertainty
        high = component + uncertainty
        if low < 0 or high > 1:
            raise ValueError("sample error interval must remain within [0,1]")

        groups.setdefault(position, []).append((sample_id, low, high))

    if "withdrawals" not in data:
        raw_withdrawals = []
    else:
        raw_withdrawals = data["withdrawals"]
    if not isinstance(raw_withdrawals, list) or len(raw_withdrawals) > 10000:
        raise ValueError("withdrawals must contain 0..10000 items")

    withdrawal_requests: list[tuple[str, str, Fraction]] = []
    seen_withdrawal_ids: set[str] = set()
    for withdrawal in raw_withdrawals:
        if not isinstance(withdrawal, dict):
            raise ValueError("each withdrawal must be an object")

        withdrawal_id = withdrawal.get("withdrawal_id")
        if not _is_nonempty_str(withdrawal_id):
            raise ValueError("withdrawal_id must be a non-empty string")
        if withdrawal_id in seen_withdrawal_ids:
            raise ValueError(f"duplicate withdrawal_id {withdrawal_id}")
        seen_withdrawal_ids.add(withdrawal_id)

        target_batch_id = withdrawal.get("batch_id")
        if target_batch_id is None:
            target_batch_id = withdrawal.get("target_batch_id")
        if not _is_nonempty_str(target_batch_id):
            raise ValueError("withdrawal batch_id must be a non-empty string")
        if target_batch_id not in batch_id_set:
            raise ValueError(f"unknown withdrawal batch_id {target_batch_id}")

        volume = _exact_amount(withdrawal.get("volume"), "volume", positive=True)
        withdrawal_requests.append((withdrawal_id, target_batch_id, volume))

    sorted_positions = sorted(groups)

    # Internal allocation state.  Records contain Fractions and are serialized
    # only after all withdrawals have been processed.
    allocation_state: dict[str, dict[str, Any]] = {}
    held_by_batch: dict[str, list[dict[str, Fraction]]] = {}

    cursor = Fraction(0)
    for row in batch_rows:
        batch_id = row["batch_id"]
        delivered = Fraction(row["delivered_volume"])
        start = cursor
        end = cursor + delivered
        cursor = end
        spec = specs[batch_id]
        spec_low = spec["min_component"]
        spec_high = spec["max_component"]
        max_gap = spec["max_gap"]

        row["spec"] = {
            "min_component": _text(spec_low),
            "max_component": _text(spec_high),
            "max_gap": _text(max_gap),
        }

        if delivered == 0:
            empty_intersections: list[dict[str, Any]] = []
            row.update(
                {
                    "status": "not_delivered",
                    "delivery_status": "not_delivered",
                    "quality_status": "not_assessed",
                    "evidence_status": "not_assessed",
                    "sufficiency_status": "not_assessed",
                    "release_status": "none",
                    "feasible_directions": [],
                    "directions": [],
                    "direction_feasibility": {
                        NONDECREASING: False,
                        NONINCREASING: False,
                        "non_decreasing": False,
                        "non_increasing": False,
                        "non-decreasing": False,
                        "non-increasing": False,
                        "ascending": False,
                        "descending": False,
                        "increasing": False,
                        "decreasing": False,
                    },
                    "nondecreasing_feasible": False,
                    "nonincreasing_feasible": False,
                    "position_intervals": empty_intersections,
                    "interval_intersections": empty_intersections,
                    "error_intersections": empty_intersections,
                    "sample_intersections": empty_intersections,
                    "sample_intervals": empty_intersections,
                    "intersections": empty_intersections,
                    "segment_assessments": [],
                    "conflict_reason": None,
                    "conflict_code": None,
                    "conflict_reasons": [],
                    "conflict": False,
                    "evidence_conflict": False,
                    "has_conflict": False,
                    "hold_reasons": [],
                    "released_volume": "0",
                    "held_volume": "0",
                    "allocated_volume": "0",
                    "remaining_released_volume": "0",
                }
            )
            allocation_state[batch_id] = {
                "records": [],
                "available": Fraction(0),
                "pointer": 0,
            }
            held_by_batch[batch_id] = []
            continue

        left_index = bisect_left(sorted_positions, start)
        right_index = bisect_right(sorted_positions, end)
        batch_positions = sorted_positions[left_index:right_index]

        intersections: list[dict[str, Any]] = []
        lowers: list[Fraction] = []
        uppers: list[Fraction] = []
        empty_position_count = 0

        for position in batch_positions:
            entries = sorted(groups[position], key=lambda item: item[0])
            sample_ids = [entry[0] for entry in entries]
            lower = max(entry[1] for entry in entries)
            upper = min(entry[2] for entry in entries)
            is_empty = lower > upper
            if is_empty:
                empty_position_count += 1

            interval_values = None if is_empty else [_text(lower), _text(upper)]
            intersection = {
                "position": _text(position),
                "sample_ids": sample_ids,
                "lower": _text(lower),
                "upper": _text(upper),
                "lower_bound": _text(lower),
                "upper_bound": _text(upper),
                "low": _text(lower),
                "high": _text(upper),
                "lower_component": _text(lower),
                "upper_component": _text(upper),
                "component_lower": _text(lower),
                "component_upper": _text(upper),
                "min_component": _text(lower),
                "max_component": _text(upper),
                "interval": interval_values,
                "error_interval": interval_values,
                "component_interval": interval_values,
                "bounds": interval_values,
                "is_empty": is_empty,
                "empty": is_empty,
            }
            intersections.append(intersection)
            lowers.append(lower)
            uppers.append(upper)

        pieces: list[dict[str, Any]] = []

        def add_piece(kind: str, piece_start: Fraction, piece_end: Fraction) -> None:
            if piece_end > piece_start:
                pieces.append(
                    {"kind": kind, "start": piece_start, "end": piece_end}
                )

        conflict_reason: str | None = None
        feasible_directions: list[str] = []
        nd_feasible = False
        ni_feasible = False
        segment_assessments: list[dict[str, Any]] = []
        hold_reasons: list[str] = []

        if empty_position_count:
            conflict_reason = "empty_error_intersection"
            hold_reasons.append(conflict_reason)
            add_piece("held", start, end)
        else:
            count = len(batch_positions)

            # Nondecreasing projections:
            # left/lower projection at i  = max(l_0 ... l_i)
            # right/upper projection at i = min(u_i ... u_n-1)
            prefix_max_lower: list[Fraction] = [Fraction(0)] * count
            current = None
            for i in range(count):
                current = lowers[i] if current is None else max(current, lowers[i])
                prefix_max_lower[i] = current

            suffix_min_upper: list[Fraction] = [Fraction(0)] * count
            current = None
            for i in range(count - 1, -1, -1):
                current = uppers[i] if current is None else min(current, uppers[i])
                suffix_min_upper[i] = current

            nd_feasible = all(
                prefix_max_lower[i] <= suffix_min_upper[i] for i in range(count)
            )

            # Nonincreasing projections:
            # left/upper projection at i = min(u_0 ... u_i)
            # right/lower projection at i = max(l_i ... l_n-1)
            prefix_min_upper: list[Fraction] = [Fraction(0)] * count
            current = None
            for i in range(count):
                current = uppers[i] if current is None else min(current, uppers[i])
                prefix_min_upper[i] = current

            suffix_max_lower: list[Fraction] = [Fraction(0)] * count
            current = None
            for i in range(count - 1, -1, -1):
                current = lowers[i] if current is None else max(current, lowers[i])
                suffix_max_lower[i] = current

            ni_feasible = all(
                suffix_max_lower[i] <= prefix_min_upper[i] for i in range(count)
            )

            if nd_feasible:
                feasible_directions.append(NONDECREASING)
            if ni_feasible:
                feasible_directions.append(NONINCREASING)

            if not feasible_directions:
                conflict_reason = "no_consistent_monotonic_direction"
                hold_reasons.append(conflict_reason)
                add_piece("held", start, end)
            else:
                if count == 0 or batch_positions[0] > start:
                    hold_reasons.append("missing_start_sample")
                    add_piece("held", start, batch_positions[0] if count else end)

                for i in range(count - 1):
                    piece_start = batch_positions[i]
                    piece_end = batch_positions[i + 1]
                    gap = piece_end - piece_start
                    gap_ok = gap <= max_gap

                    direction_envelopes: dict[str, dict[str, Any]] = {}
                    envelope_fractions: dict[str, tuple[Fraction, Fraction]] = {}
                    failure_reasons: list[str] = []

                    if not gap_ok:
                        failure_reasons.append("max_gap_exceeded")

                    if nd_feasible:
                        env_low = prefix_max_lower[i]
                        env_high = suffix_min_upper[i + 1]
                        envelope_fractions[NONDECREASING] = (env_low, env_high)
                        lower_ok = env_low >= spec_low
                        upper_ok = env_high <= spec_high
                        within_spec = lower_ok and upper_ok
                        if not within_spec:
                            failure_reasons.append(
                                f"{NONDECREASING}_envelope_outside_spec"
                            )
                        direction_envelopes[NONDECREASING] = {
                            "feasible": True,
                            "lower_envelope": _text(env_low),
                            "upper_envelope": _text(env_high),
                            "lower_within_spec": lower_ok,
                            "upper_within_spec": upper_ok,
                            "within_spec": within_spec,
                        }

                    if ni_feasible:
                        env_low = suffix_max_lower[i + 1]
                        env_high = prefix_min_upper[i]
                        envelope_fractions[NONINCREASING] = (env_low, env_high)
                        lower_ok = env_low >= spec_low
                        upper_ok = env_high <= spec_high
                        within_spec = lower_ok and upper_ok
                        if not within_spec:
                            failure_reasons.append(
                                f"{NONINCREASING}_envelope_outside_spec"
                            )
                        direction_envelopes[NONINCREASING] = {
                            "feasible": True,
                            "lower_envelope": _text(env_low),
                            "upper_envelope": _text(env_high),
                            "lower_within_spec": lower_ok,
                            "upper_within_spec": upper_ok,
                            "within_spec": within_spec,
                        }

                    # When both batch-level directions are feasible, the true
                    # curve may belong to either.  The pointwise possible range
                    # is therefore the union of their envelopes.
                    conservative_low = min(
                        bounds[0] for bounds in envelope_fractions.values()
                    )
                    conservative_high = max(
                        bounds[1] for bounds in envelope_fractions.values()
                    )
                    conservative_lower_ok = conservative_low >= spec_low
                    conservative_upper_ok = conservative_high <= spec_high
                    conservative_within_spec = (
                        conservative_lower_ok and conservative_upper_ok
                    )

                    releasable = gap_ok and conservative_within_spec

                    assessment = {
                        "start": _text(piece_start),
                        "end": _text(piece_end),
                        "gap": _text(gap),
                        "max_gap": _text(max_gap),
                        "gap_ok": gap_ok,
                        "directions": direction_envelopes,
                        "conservative_lower_envelope": _text(conservative_low),
                        "conservative_upper_envelope": _text(conservative_high),
                        "conservative_lower_within_spec": conservative_lower_ok,
                        "conservative_upper_within_spec": conservative_upper_ok,
                        "conservative_within_spec": conservative_within_spec,
                        "failure_reasons": failure_reasons,
                        "releasable": releasable,
                    }
                    segment_assessments.append(assessment)

                    if releasable:
                        add_piece("released", piece_start, piece_end)
                    else:
                        for reason in failure_reasons:
                            if reason not in hold_reasons:
                                hold_reasons.append(reason)
                        add_piece("held", piece_start, piece_end)

                if count > 0 and batch_positions[-1] < end:
                    if "missing_end_sample" not in hold_reasons:
                        hold_reasons.append("missing_end_sample")
                    add_piece("held", batch_positions[-1], end)

        # Merge adjacent pieces with the same state.
        merged_pieces: list[dict[str, Any]] = []
        for piece in pieces:
            if (
                merged_pieces
                and merged_pieces[-1]["kind"] == piece["kind"]
                and merged_pieces[-1]["end"] == piece["start"]
            ):
                merged_pieces[-1]["end"] = piece["end"]
            else:
                merged_pieces.append(dict(piece))

        released_records: list[dict[str, Any]] = []
        held_records: list[dict[str, Fraction]] = []
        released_volume = Fraction(0)
        held_volume = Fraction(0)

        release_index = 0
        for piece in merged_pieces:
            volume = piece["end"] - piece["start"]
            if piece["kind"] == "released":
                released_records.append(
                    {
                        "interval_index": release_index,
                        "start": piece["start"],
                        "end": piece["end"],
                        "volume": volume,
                        "allocated": Fraction(0),
                    }
                )
                release_index += 1
                released_volume += volume
            else:
                held_records.append(
                    {"start": piece["start"], "end": piece["end"], "volume": volume}
                )
                held_volume += volume

        if conflict_reason is not None:
            status = "conflict"
            release_status = "conflict"
            sufficiency_status = "conflict"
        else:
            sampling_reasons = {
                "missing_start_sample",
                "missing_end_sample",
                "no_samples",
                "max_gap_exceeded",
            }
            if any(reason in sampling_reasons for reason in hold_reasons):
                sufficiency_status = "insufficient_evidence"
            else:
                sufficiency_status = "sufficient_evidence"

            if released_volume == 0:
                status = "held"
                release_status = "none_released"
            elif released_volume == delivered:
                status = "released"
                release_status = "fully_released"
            else:
                status = "partially_released"
                release_status = "partially_released"

        direction_feasibility = {
            NONDECREASING: nd_feasible if conflict_reason is None else False,
            NONINCREASING: ni_feasible if conflict_reason is None else False,
            "non_decreasing": nd_feasible if conflict_reason is None else False,
            "non_increasing": ni_feasible if conflict_reason is None else False,
            "non-decreasing": nd_feasible if conflict_reason is None else False,
            "non-increasing": ni_feasible if conflict_reason is None else False,
            # Compatibility aliases for common terminology.
            "ascending": nd_feasible if conflict_reason is None else False,
            "descending": ni_feasible if conflict_reason is None else False,
            "increasing": nd_feasible if conflict_reason is None else False,
            "decreasing": ni_feasible if conflict_reason is None else False,
        }

        row.update(
            {
                "status": status,
                "delivery_status": "delivered",
                "quality_status": "conflict" if conflict_reason else "ok",
                "evidence_status": "conflict" if conflict_reason else "consistent",
                "sufficiency_status": sufficiency_status,
                "release_status": release_status,
                "feasible_directions": feasible_directions,
                "directions": feasible_directions,
                "nondecreasing_feasible": nd_feasible if conflict_reason is None else False,
                "nonincreasing_feasible": ni_feasible if conflict_reason is None else False,
                "direction_feasibility": direction_feasibility,
                "position_intervals": intersections,
                "interval_intersections": intersections,
                "error_intersections": intersections,
                "sample_intersections": intersections,
                "sample_intervals": intersections,
                "intersections": intersections,
                "segment_assessments": segment_assessments,
                "conflict_reason": conflict_reason,
                "conflict_code": conflict_reason,
                "conflict_reasons": [conflict_reason] if conflict_reason else [],
                "conflict": conflict_reason is not None,
                "evidence_conflict": conflict_reason is not None,
                "has_conflict": conflict_reason is not None,
                "hold_reasons": hold_reasons,
                "released_volume": _text(released_volume),
                "held_volume": _text(held_volume),
            }
        )

        allocation_state[batch_id] = {
            "records": released_records,
            "available": released_volume,
            "pointer": 0,
        }
        held_by_batch[batch_id] = held_records

    # Withdrawals are validated above; now process them in input order.
    withdrawal_results: list[dict[str, Any]] = []
    for withdrawal_id, batch_id, volume in withdrawal_requests:
        state = allocation_state[batch_id]
        available = state["available"]

        if volume > available:
            withdrawal_results.append(
                {
                    "withdrawal_id": withdrawal_id,
                    "id": withdrawal_id,
                    "batch_id": batch_id,
                    "target_batch_id": batch_id,
                    "requested_volume": _text(volume),
                    "volume": _text(volume),
                    "status": "unfulfilled",
                    "fulfilled": False,
                    "allocated_volume": "0",
                    "available_released_volume": _text(available),
                    "shortfall_volume": _text(volume - available),
                    "segments": [],
                    "sources": [],
                    "allocations": [],
                }
            )
            continue

        segments: list[dict[str, Any]] = []
        remaining = volume
        pointer = state["pointer"]
        records = state["records"]

        while remaining > 0:
            record = records[pointer]
            free_volume = record["volume"] - record["allocated"]
            take = min(free_volume, remaining)

            segment_start = record["start"] + record["allocated"]
            segment_end = segment_start + take
            segment = {
                "interval_index": record["interval_index"],
                "source_interval_index": record["interval_index"],
                "released_interval_index": record["interval_index"],
                "start": _text(segment_start),
                "end": _text(segment_end),
                "segment_start": _text(segment_start),
                "segment_end": _text(segment_end),
                "source_start": _text(segment_start),
                "source_end": _text(segment_end),
                "source_interval_start": _text(record["start"]),
                "source_interval_end": _text(record["end"]),
                "volume": _text(take),
                "batch_id": batch_id,
            }
            segments.append(segment)

            record["allocated"] += take
            remaining -= take
            if record["allocated"] == record["volume"]:
                pointer += 1

        state["pointer"] = pointer
        state["available"] = available - volume

        withdrawal_result = {
            "withdrawal_id": withdrawal_id,
            "id": withdrawal_id,
            "batch_id": batch_id,
            "target_batch_id": batch_id,
            "requested_volume": _text(volume),
            "volume": _text(volume),
            "status": "fulfilled",
            "fulfilled": True,
            "allocated_volume": _text(volume),
            "segments": segments,
            "sources": segments,
            "allocations": segments,
        }
        withdrawal_results.append(withdrawal_result)

    # Serialize final interval allocation state onto batch rows.
    for row in batch_rows:
        batch_id = row["batch_id"]
        state = allocation_state[batch_id]
        released_json: list[dict[str, Any]] = []
        batch_allocated = Fraction(0)

        for record in state["records"]:
            allocated = record["allocated"]
            remaining = record["volume"] - allocated
            batch_allocated += allocated
            released_json.append(
                {
                    "interval_index": record["interval_index"],
                    **_interval_json(record["start"], record["end"]),
                    "allocated_volume": _text(allocated),
                    "remaining_volume": _text(remaining),
                    "allocated": _text(allocated),
                    "remaining": _text(remaining),
                }
            )

        held_json = [
            _interval_json(record["start"], record["end"])
            for record in held_by_batch[batch_id]
        ]

        row["released_intervals"] = released_json
        row["held_intervals"] = held_json
        row["allocated_volume"] = _text(batch_allocated)
        row["remaining_released_volume"] = _text(state["available"])

    batch_by_id = {row["batch_id"]: row for row in batch_rows}
    result["withdrawals"] = withdrawal_results
    result["withdrawal_results"] = withdrawal_results
    result["withdrawal_allocations"] = withdrawal_results
    result["allocations"] = withdrawal_results
    result["qualifications"] = batch_rows
    result["batch_qualifications"] = batch_rows
    result["batch_results"] = batch_rows
    result["qualified_batches"] = batch_rows
    result["batch_by_id"] = batch_by_id
    result["qualifications_by_batch"] = batch_by_id
    return result
