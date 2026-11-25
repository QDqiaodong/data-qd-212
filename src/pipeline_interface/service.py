from __future__ import annotations

from fractions import Fraction


def _num(value: object, name: str, positive: bool = False) -> Fraction:
    if not isinstance(value, str) or not value or len(value) > 32:
        raise ValueError(f"{name} must be a decimal string")
    try:
        result = Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"invalid {name}") from exc
    if result < 0 or (positive and result == 0):
        raise ValueError(f"invalid {name}")
    return result


def _text(value: Fraction) -> str:
    return str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"


def process(data: dict) -> dict:
    batches = data.get("batches") if isinstance(data, dict) else None
    meters = data.get("meters") if isinstance(data, dict) else None
    loss = _num(data.get("loss_permille"), "loss_permille")
    if loss > 1000:
        raise ValueError("loss_permille exceeds 1000")
    if not isinstance(batches, list) or not 1 <= len(batches) <= 1000:
        raise ValueError("batches must contain 1..1000 items")
    if not isinstance(meters, list) or not 2 <= len(meters) <= 10000:
        raise ValueError("meters must contain 2..10000 readings")

    batch_ids: set[str] = set()
    sequence = []
    total_nominal = Fraction(0)
    for item in batches:
        bid = item.get("id") if isinstance(item, dict) else None
        if not isinstance(bid, str) or not bid or bid in batch_ids:
            raise ValueError("batch ids must be unique")
        volume = _num(item.get("nominal_volume"), "nominal_volume", positive=True)
        batch_ids.add(bid); total_nominal += volume; sequence.append((bid, volume))

    readings = []
    meter_ids: set[str] = set()
    last_position = None
    last_cumulative = None
    for meter in meters:
        mid = meter.get("id") if isinstance(meter, dict) else None
        if not isinstance(mid, str) or not mid or mid in meter_ids:
            raise ValueError("meter ids must be unique")
        position = _num(meter.get("position_km"), "position_km")
        cumulative = _num(meter.get("cumulative_volume"), "cumulative_volume")
        if last_position is not None and position <= last_position:
            raise ValueError("meters must be strictly ordered by position")
        if last_cumulative is not None and cumulative > last_cumulative:
            raise ValueError("downstream cumulative volume cannot increase")
        meter_ids.add(mid); readings.append((mid, position, cumulative))
        last_position, last_cumulative = position, cumulative
    if readings[0][2] != total_nominal:
        raise ValueError("first meter must equal total nominal batch volume")

    segments = []
    for left, right in zip(readings, readings[1:]):
        observed_loss = left[2] - right[2]
        allowed_loss = left[2] * loss / 1000
        segments.append({
            "from_meter": left[0], "to_meter": right[0],
            "observed_loss": _text(observed_loss), "allowed_loss": _text(allowed_loss),
            "status": "within_limit" if observed_loss <= allowed_loss else "excess_loss",
        })

    remaining = readings[-1][2]
    delivered = []
    interfaces = []
    cursor = Fraction(0)
    for index, (bid, nominal) in enumerate(sequence):
        accepted = min(nominal, remaining)
        delivered.append({"batch_id": bid, "nominal_volume": _text(nominal), "delivered_volume": _text(accepted), "shortfall": _text(nominal - accepted)})
        cursor += accepted
        remaining -= accepted
        if index < len(sequence) - 1 and accepted == nominal:
            interfaces.append({"upstream_batch": bid, "downstream_batch": sequence[index + 1][0], "delivered_position": _text(cursor)})
    return {"segments": segments, "batches": delivered, "interfaces": interfaces, "terminal_volume": _text(readings[-1][2])}


def __getattr__(name: str):
    # Preserve service.py as the accounting module while also allowing
    # ``from pipeline_interface.service import qualify``.
    if name == "qualify":
        from .qualification import qualify

        return qualify
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

