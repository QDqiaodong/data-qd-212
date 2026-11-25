# Pipeline batch interface contract

Pipeline schedulers provide an ordered list of 1..1000 product batches and 2..10000 cumulative-volume meter readings ordered by strictly increasing kilometre position. Batch and meter IDs are unique. Volumes and positions are bounded nonnegative decimal strings represented as exact rational numbers. The first cumulative reading must equal total nominated batch volume; downstream cumulative readings may only fall.

Each adjacent meter pair reports observed loss and a permitted loss derived from loss_permille. The terminal cumulative volume is allocated to batches in nomination order, preserving delivered volume, shortfall and each completely crossed batch interface. The baseline assumes piston-like ordered flow and does not model mixing zones, density correction, withdrawals, reverse flow or time-varying rate.

The result is a structured reconciliation used by terminal accounting, not inventory CRUD, a generic report or a physical control system.

## Offline quality qualification API

`qualify(data) -> dict` first calls `process(data)` and therefore preserves the existing `process` input, output and `ValueError` contract. It does not mutate `data`. The returned mapping contains the existing process keys; each row in `batches` is augmented with quality results, and `withdrawals` contains allocation results in request order.

Additional input fields:

- `specs`: one specification per batch, either a list containing `batch_id` or an object mapping batch ID to specification. Each specification has decimal strings `min_component` and `max_component` in `[0,1]` with `min_component <= max_component`, plus positive exact `max_gap`.
- `samples`: 1..20000 objects with globally unique `sample_id`, decimal strings `position`, `component`, `uncertainty`. `uncertainty >= 0`, `component - uncertainty >= 0`, `component + uncertainty <= 1`, and positions must be in `[0, terminal_volume]`.
- `withdrawals`: optional list of at most 10000 requests with unique `withdrawal_id`, existing `batch_id`, and positive `volume`.

Structural, identity, decimal, specification-coverage or reference errors raise `ValueError` and reject the whole call. Conflicting evidence, insufficient sampling and insufficient releasable volume are normal result states.

### Exact delivery axis

All coordinates are exact `fractions.Fraction` values internally and are serialized with the existing reduced rational string convention. Equal rational positions are grouped regardless of decimal spelling such as `60`, `60.0` and `60.00`.

Batch delivery intervals partition `[0, terminal_volume]` according to accounted `delivered_volume`. A sample at a common boundary is included by both adjacent delivered batches; every other sample belongs to its containing batch. A batch with zero delivered volume has `status="not_delivered"` and empty interval/evidence sets.

### Position intersections and monotonic directions

At one position, the feasible component interval is the intersection of all sample error intervals at that rational position. If `lower > upper`, that batch has `conflict_reason="empty_error_intersection"` and its complete delivered interval is held.

For non-empty intersections `[l_i,u_i]` at ordered positions `p_i`:

- Nondecreasing curves are globally feasible iff
  `max(l_0..l_i) <= min(u_i..u_n-1)` for every `i`.
- Nonincreasing curves are globally feasible iff
  `max(l_i..l_n-1) <= min(u_0..u_i)` for every `i`.

If neither direction is feasible, `conflict_reason="no_consistent_monotonic_direction"` and the complete batch is held. `feasible_directions` contains `nondecreasing` and/or `nonincreasing`. A direction is a batch-level choice; adjacent gaps cannot switch direction.

### Conservative envelopes

For a feasible nondecreasing direction, every curve on gap `[p_i,p_i+1]` is contained by

```text
[ max(l_0..l_i), min(u_i+1..u_n-1) ]
```

For a feasible nonincreasing direction, every curve on that gap is contained by

```text
[ max(l_i+1..l_n-1), min(u_0..u_i) ].
```

If both directions are feasible, release requires the specification to contain the union of both directional envelopes. Nominal readings, linear interpolation and a locally consistent pair are not substitutes for these global projections.

A positive-volume release segment must be between two available sample positions with `p_i+1-p_i <= max_gap` and a conservative envelope contained in `[min_component,max_component]`. Missing batch endpoints and over-distance gaps remain held; there is no extrapolation. Adjacent segments with the same released/held state are merged. `released_intervals` and `held_intervals` are sorted half-open `[start,end)` intervals and cover all delivered volume without overlap. The terminal coordinate is the only endpoint that may be understood as a closed physical end.

Batch rows include:

- `status`: `not_delivered`, `conflict`, `held`, `partially_released` or `released`;
- `feasible_directions` and `direction_feasibility`;
- `position_intervals` (also exposed as `interval_intersections`, `error_intersections`, `sample_intersections` and `intersections`);
- `conflict_reason` / `conflict_code`;
- `segment_assessments`, including exact directional and conservative envelopes;
- `released_intervals`, `held_intervals`, `released_volume` and `held_volume`.

### Withdrawal allocation

Withdrawals are validated as one batch before allocation. They are then processed in input order. Each request consumes unallocated released volume of its named batch from low to high delivery coordinates and may split across released intervals.

If the complete requested volume is unavailable, the result has `status="unfulfilled"` and nothing from that request is consumed. Earlier successful allocations remain committed, and later requests may use the untouched remaining volume. Successful results have `status="fulfilled"` and exact `segments` (also aliased as `sources` and `allocations`) with `interval_index`, `start`, `end` and `volume`. Every released interval reports `allocated_volume` and `remaining_volume`.

Run the offline example with:

```bash
python examples/run_qualify_sample.py
```
