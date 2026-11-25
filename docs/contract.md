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

## Offline campaign planning API

`plan_campaign(data) -> dict` plans at most one occurrence of each candidate batch and returns the executable schedule together with the complete waste ledger. It does not mutate `data`, control equipment, or alter `process` / `qualify`.

All volumes are parsed as exact `fractions.Fraction` values. Internally the planner also uses one common power-of-ten integer scale for capacity and flush totals, so no binary floating-point value decides a boundary.

### Input contract

- `flow_rate`: positive decimal string.
- `horizon_minutes`: positive integer.
- `batches`: 1..18 objects with unique nonempty string `id`, positive decimal string `volume`, integer `release_minute` / `deadline_minute`, and integer `priority` in `1..5`. Times satisfy `0 <= release_minute < deadline_minute <= horizon_minutes`.
- `transitions`: directed unique `(from,to)` edges. `from` is `START` or an existing batch ID; `to` is an existing batch ID. `START` cannot be a target, self-edges are invalid, and a missing edge forbids that direct adjacency. Each edge has nonnegative decimal string `flush_volume` and nonnegative integer `flush_minutes`.
- Environmental fields: nonnegative exact `initial_waste`, positive exact `waste_capacity`, 0..18 `drains`, and 0..18 `committed_inflows`. Integral Python values are accepted for environmental amounts because they are exact; floats are rejected.
- A drain has unique integer `minute` in `0..horizon_minutes` and positive `amount`.
- A committed inflow has globally unique nonempty `event_id`, valid minute, positive `amount`, and nonempty `source_ref`.

Structural, identity, reference, decimal, priority or time-window errors raise `ValueError` for the whole call. Baseline overflow and candidate-plan infeasibility are normal returned states.

### Exact minutes and waiting

For batch volume `V` and flow rate `R`, duration is `ceil(V/R)` minutes. A transition duration is `flush_minutes + ceil(flush_volume/R)`. The `START -> first batch` transition is timed like every other transition.

For a fixed sequence, the planner uses the earliest feasible times. Waiting occurs before starting a transition and may be required by:

1. the target batch release minute;
2. waste capacity at the transition end minute.

The transition end must leave enough time for the target batch to complete at or before its deadline and the horizon. The search checks the earliest lower bound and later drain minutes; inflows only increase waste, so they cannot create capacity. It never searches every permutation or every minute of a large horizon.

### Waste ledger semantics

The ledger starts with an `entry_type="initial_waste"` balance row. If `initial_waste > waste_capacity`, the result is `environment_status="baseline_conflict"` and the first conflict is recorded before minute-zero processing. A minute-zero drain cannot retroactively repair that initial state.

At each minute, rows are produced in this mandatory order:

1. fixed drain, removing `min(amount, current_balance)`;
2. all committed inflows, displayed in `event_id` order for equal minutes, with their total used for the capacity decision;
3. the selected campaign transition flush, if one ends at that minute.

A baseline conflict returns an empty campaign plan while retaining every fixed drain/inflow and the complete fixed ledger. The conflict code is either `initial_waste_over_capacity` or `committed_inflows_over_capacity`. If the baseline is compliant but no candidate sequence can accommodate its new flushes, the result is `status="new_plan_infeasible"` with `environment_status="compliant"`.

### Optimization

Feasible subset/order/start-time plans are compared lexicographically by:

1. planned-count vector `(priority_1_count, ..., priority_5_count)`, maximizing component 1 first;
2. total flush volume, minimizing;
3. sum of scheduled batch completion minutes, minimizing;
4. batch ID sequence, lexicographically smallest.

Thus no lower-priority batch can displace a retainable higher-priority batch.

The implementation uses subset dynamic programming over `(mask,last_batch)` with a small Pareto frontier for time, objective prefix and waste state. This avoids both factorial permutation enumeration and unbounded minute-by-minute waiting searches.

### Result shape

The stable high-level keys are:

- `environment_status`: `"compliant"` or `"baseline_conflict"`;
- `status`: `"feasible"`, `"new_plan_infeasible"` or `"baseline_conflict"`;
- `plan_status`: `"optimal"`, `"new_plan_infeasible"` or `"baseline_conflict"`;
- `sequence` / `selected_batch_ids`: selected IDs in execution order;
- `plan` / `new_plan` (also aliased as `campaign`, `campaign_plan` and `schedule`) is a list of executable scheduled batch rows. For compatibility it also exposes mapping-style metadata such as `sequence`, `batches`, `transitions`, `priority_counts`, `total_flush_volume` and `total_completion_minutes`; `plan_detail` is the same metadata as a plain dictionary;
- `batches` / `candidates`: every candidate sorted by ID. Selected rows have `status="scheduled"` and start/completion fields plus an embedded `preceding_transition`; unselected rows have `status="not_selected"`;
- `waste_ledger` / `ledger`: initial row plus ordered drain, committed-inflow and selected-transition-flush rows;
- `drains` and `committed_inflows`: canonical fixed events;
- `first_conflict` / `conflict`: first baseline conflict or `null`;
- `baseline`: baseline status, capacity and initial balance.

Volume strings are exact finite decimals. Minute and duration fields are JSON integers. Input array order does not affect canonical scheduling or ledger output.

Run the offline example with:

```bash
python examples/run_campaign_sample.py
```
