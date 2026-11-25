from __future__ import annotations

import copy
import itertools
import random
import time
import unittest
from fractions import Fraction

from pipeline_interface import plan_campaign


def ceil_div(value: Fraction, rate: Fraction) -> int:
    return -((-value) // rate)


class CampaignAcceptanceTests(unittest.TestCase):
    def test_committed_move_across_drain_changes_feasibility_not_identity(self):
        base = {
            "flow_rate": "10",
            "horizon_minutes": 10,
            "batches": [
                {
                    "id": "H",
                    "volume": "10",
                    "release_minute": 0,
                    "deadline_minute": 2,
                    "priority": 1,
                }
            ],
            "transitions": [
                {
                    "from": "START",
                    "to": "H",
                    "flush_volume": "5",
                    "flush_minutes": 0,
                }
            ],
            "initial_waste": "0",
            "waste_capacity": "9",
            "drains": [{"minute": 3, "amount": "1"}],
            "committed_inflows": [
                {
                    "event_id": "commit-1",
                    "minute": 4,
                    "amount": "5",
                    "source_ref": "left-tank",
                }
            ],
        }

        before = plan_campaign(copy.deepcopy(base))
        self.assertEqual(before["environment_status"], "compliant")
        self.assertEqual(before["sequence"], ["H"])
        self.assertEqual([row["batch_id"] for row in before["new_plan"]], ["H"])
        self.assertEqual(before["new_plan"]["sequence"], ["H"])

        moved = copy.deepcopy(base)
        moved["committed_inflows"][0]["minute"] = 0
        after = plan_campaign(moved)
        self.assertEqual(after["environment_status"], "compliant")
        self.assertEqual(after["status"], "new_plan_infeasible")
        self.assertEqual(after["sequence"], [])
        row = after["batches"][0]
        self.assertEqual(row["status"], "not_selected")
        self.assertEqual(row["reason"], "new_plan_infeasible")
        self.assertFalse(row["committable"])

        for result in (before, after):
            events = result["committed_inflows"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event_id"], "commit-1")
            self.assertEqual(events[0]["amount"], "5")
            self.assertEqual(events[0]["source_ref"], "left-tank")

        overflow = copy.deepcopy(base)
        overflow["committed_inflows"][0]["amount"] = "10"
        conflict = plan_campaign(overflow)
        self.assertEqual(conflict["environment_status"], "baseline_conflict")
        self.assertEqual(conflict["status"], "baseline_conflict")
        self.assertEqual(conflict["plan_status"], "baseline_conflict")
        self.assertEqual(conflict["sequence"], [])
        self.assertEqual(conflict["new_plan"], [])
        self.assertEqual(conflict["first_conflict"]["code"], "committed_inflows_over_capacity")
        self.assertTrue(
            any(e["entry_type"] == "committed_inflow" for e in conflict["waste_ledger"])
        )

    def test_same_minute_order_is_drain_committed_then_new_flush(self):
        data = {
            "flow_rate": "100",
            "horizon_minutes": 10,
            "batches": [
                {
                    "id": "b",
                    "volume": "100",
                    "release_minute": 0,
                    "deadline_minute": 6,
                    "priority": 1,
                }
            ],
            "transitions": [
                {
                    "from": "START",
                    "to": "b",
                    "flush_volume": "28",
                    "flush_minutes": 4,
                }
            ],
            "initial_waste": "10",
            "waste_capacity": "100",
            "drains": [{"minute": 5, "amount": "8"}],
            "committed_inflows": [
                {"event_id": "z", "minute": 5, "amount": "40", "source_ref": "z"},
                {"event_id": "a", "minute": 5, "amount": "30", "source_ref": "a"},
            ],
        }
        result = plan_campaign(data)
        self.assertEqual(result["sequence"], ["b"])
        minute_five = [e for e in result["waste_ledger"] if e["minute"] == 5]
        self.assertEqual(
            [e["entry_type"] for e in minute_five],
            ["drain", "committed_inflow", "committed_inflow", "campaign_transition_inflow"],
        )
        self.assertEqual(minute_five[1]["event_id"], "a")
        self.assertEqual(minute_five[2]["event_id"], "z")
        self.assertEqual([e["balance"] for e in minute_five], ["2", "32", "72", "100"])

        reordered = copy.deepcopy(data)
        reordered["committed_inflows"].reverse()
        result_reordered = plan_campaign(reordered)
        self.assertEqual(
            result_reordered["waste_ledger"], result["waste_ledger"]
        )

        initial_over = {
            "flow_rate": "10",
            "horizon_minutes": 4,
            "batches": [],
            "transitions": [],
            "initial_waste": "101",
            "waste_capacity": "100",
            "drains": [{"minute": 0, "amount": "100"}],
            "committed_inflows": [],
        }
        # Input contract says 1..18 batches.  Add one harmless batch to focus
        # this assertion on the environmental baseline.
        initial_over["batches"] = [
            {"id": "x", "volume": "1", "release_minute": 0, "deadline_minute": 1, "priority": 1}
        ]
        conflict = plan_campaign(initial_over)
        self.assertEqual(conflict["environment_status"], "baseline_conflict")
        self.assertEqual(conflict["first_conflict"]["code"], "initial_waste_over_capacity")
        minute_zero = [e for e in conflict["waste_ledger"] if e["minute"] == 0]
        self.assertEqual(minute_zero[0]["entry_type"], "initial_waste")
        drain_entry = next(e for e in minute_zero if e["entry_type"] == "drain")
        self.assertEqual(drain_entry["removed_amount"], "100")
        self.assertEqual(drain_entry["balance"], "1")

    def test_priority_is_protected_before_flush_and_time(self):
        data = {
            "flow_rate": "100",
            "horizon_minutes": 20,
            "batches": [
                {"id": "H", "volume": "100", "release_minute": 0, "deadline_minute": 3, "priority": 1},
                {"id": "L", "volume": "100", "release_minute": 0, "deadline_minute": 20, "priority": 2},
            ],
            "transitions": [
                {"from": "START", "to": "H", "flush_volume": "50", "flush_minutes": 0},
                {"from": "START", "to": "L", "flush_volume": "0", "flush_minutes": 0},
                {"from": "L", "to": "H", "flush_volume": "0", "flush_minutes": 5},
            ],
            "initial_waste": "0",
            "waste_capacity": "1000",
            "drains": [],
            "committed_inflows": [],
        }
        result = plan_campaign(data)
        self.assertEqual(result["sequence"], ["H"])
        self.assertEqual(result["priority_counts"], [1, 0, 0, 0, 0])

    def test_flush_beats_completion_sum_and_then_lex_sequence(self):
        flush_time_tradeoff = {
            "flow_rate": "100",
            "horizon_minutes": 20,
            "batches": [
                {"id": "a", "volume": "100", "release_minute": 0, "deadline_minute": 20, "priority": 1},
                {"id": "b", "volume": "100", "release_minute": 0, "deadline_minute": 20, "priority": 1},
            ],
            "transitions": [
                {"from": "START", "to": "a", "flush_volume": "0", "flush_minutes": 0},
                {"from": "a", "to": "b", "flush_volume": "0", "flush_minutes": 5},
                {"from": "START", "to": "b", "flush_volume": "1", "flush_minutes": 0},
                {"from": "b", "to": "a", "flush_volume": "1", "flush_minutes": 0},
            ],
            "initial_waste": "0",
            "waste_capacity": "1000",
            "drains": [],
            "committed_inflows": [],
        }
        result = plan_campaign(flush_time_tradeoff)
        self.assertEqual(result["sequence"], ["a", "b"])
        self.assertEqual(result["total_flush_volume"], "0")
        self.assertEqual(result["total_completion_minutes"], 8)

        lex_tie = copy.deepcopy(flush_time_tradeoff)
        lex_tie["transitions"] = [
            {"from": "START", "to": "a", "flush_volume": "0", "flush_minutes": 0},
            {"from": "START", "to": "b", "flush_volume": "0", "flush_minutes": 0},
            {"from": "a", "to": "b", "flush_volume": "0", "flush_minutes": 0},
            {"from": "b", "to": "a", "flush_volume": "0", "flush_minutes": 0},
        ]
        tied = plan_campaign(lex_tie)
        self.assertEqual(tied["sequence"], ["a", "b"])
        self.assertEqual(tied["total_flush_volume"], "0")
        self.assertEqual(tied["total_completion_minutes"], 3)

    def test_boundaries_ceil_waiting_and_drain_amount_over_balance(self):
        exact = {
            "flow_rate": "10",
            "horizon_minutes": 10,
            "batches": [
                {"id": "x", "volume": "30", "release_minute": 2, "deadline_minute": 5, "priority": 1}
            ],
            "transitions": [
                {"from": "START", "to": "x", "flush_volume": "0", "flush_minutes": 2}
            ],
            "initial_waste": "0",
            "waste_capacity": "100",
            "drains": [],
            "committed_inflows": [],
        }
        row = plan_campaign(exact)["plan"]["batches"][0]
        self.assertEqual(row["start_minute"], 2)
        self.assertEqual(row["complete_minute"], 5)
        transition = row["preceding_transition"]
        self.assertEqual(transition["start_minute"], 0)
        self.assertEqual(transition["end_minute"], 2)

        ceil_case = copy.deepcopy(exact)
        ceil_case["batches"][0].update(release_minute=0, volume="10", deadline_minute=2)
        ceil_case["transitions"][0].update(flush_volume="11", flush_minutes=0)
        # ceil(11/10)=2 transition minutes, but transition must end by minute 1.
        infeasible = plan_campaign(ceil_case)
        self.assertEqual(infeasible["sequence"], [])
        self.assertEqual(infeasible["status"], "new_plan_infeasible")

        wait_case = {
            "flow_rate": "100",
            "horizon_minutes": 10,
            "batches": [
                {"id": "w", "volume": "1", "release_minute": 0, "deadline_minute": 8, "priority": 1}
            ],
            "transitions": [
                {"from": "START", "to": "w", "flush_volume": "5", "flush_minutes": 0}
            ],
            "initial_waste": "8",
            "waste_capacity": "10",
            "drains": [{"minute": 3, "amount": "8"}],
            "committed_inflows": [],
        }
        waited = plan_campaign(wait_case)
        transition = waited["plan"]["transitions"][0]
        self.assertEqual(transition["end_minute"], 3)
        self.assertEqual(transition["start_minute"], 2)
        self.assertEqual(transition["waiting_minutes"], 2)

        over_drain = plan_campaign(
            {
                "flow_rate": "1",
                "horizon_minutes": 2,
                "batches": [
                    {"id": "z", "volume": "1", "release_minute": 0, "deadline_minute": 2, "priority": 1}
                ],
                "transitions": [],
                "initial_waste": "3",
                "waste_capacity": "10",
                "drains": [{"minute": 0, "amount": "10"}],
                "committed_inflows": [],
            }
        )
        drain_entry = next(e for e in over_drain["waste_ledger"] if e["entry_type"] == "drain")
        self.assertEqual(drain_entry["requested_amount"], "10")
        self.assertEqual(drain_entry["removed_amount"], "3")
        self.assertEqual(drain_entry["balance"], "0")

    def test_missing_edge_forbids_adjacency_and_empty_plan_has_full_ledger(self):
        data = {
            "flow_rate": "10",
            "horizon_minutes": 10,
            "batches": [
                {"id": "a", "volume": "1", "release_minute": 0, "deadline_minute": 10, "priority": 1},
                {"id": "b", "volume": "1", "release_minute": 0, "deadline_minute": 10, "priority": 1},
            ],
            "transitions": [
                {"from": "START", "to": "a", "flush_volume": "0", "flush_minutes": 0},
                {"from": "START", "to": "b", "flush_volume": "0", "flush_minutes": 0},
            ],
            "initial_waste": "1",
            "waste_capacity": "10",
            "drains": [{"minute": 2, "amount": "1"}],
            "committed_inflows": [
                {"event_id": "e", "minute": 1, "amount": "1", "source_ref": "s"}
            ],
        }
        result = plan_campaign(data)
        self.assertEqual(result["sequence"], ["a"])  # lex tie among equal singletons

        empty = copy.deepcopy(data)
        empty["transitions"] = []
        empty_result = plan_campaign(empty)
        self.assertEqual(empty_result["sequence"], [])
        self.assertEqual(empty_result["new_plan"], [])
        self.assertEqual(empty_result["status"], "new_plan_infeasible")
        types = [e["entry_type"] for e in empty_result["waste_ledger"]]
        self.assertIn("initial_waste", types)
        self.assertIn("drain", types)
        self.assertIn("committed_inflow", types)
        self.assertNotIn("campaign_transition_inflow", types)

    def test_invalid_structures_and_references_raise_value_error(self):
        valid = {
            "flow_rate": "10",
            "horizon_minutes": 10,
            "batches": [
                {"id": "a", "volume": "1", "release_minute": 0, "deadline_minute": 2, "priority": 1}
            ],
            "transitions": [
                {"from": "START", "to": "a", "flush_volume": "0", "flush_minutes": 0}
            ],
            "initial_waste": "0",
            "waste_capacity": "10",
            "drains": [{"minute": 1, "amount": "1"}],
            "committed_inflows": [
                {"event_id": "e0", "minute": 2, "amount": "1", "source_ref": "s"}
            ],
        }

        def mutate(fn):
            data = copy.deepcopy(valid)
            fn(data)
            with self.assertRaises(ValueError):
                plan_campaign(data)

        mutate(lambda d: d.update(flow_rate=10))
        mutate(lambda d: d.update(horizon_minutes=0))
        mutate(lambda d: d["batches"][0].update(priority=6))
        mutate(lambda d: d["batches"][0].update(deadline_minute=11))
        mutate(lambda d: d["batches"][0].update(release_minute=3, deadline_minute=2))
        mutate(lambda d: d["batches"].append(copy.deepcopy(d["batches"][0])))
        mutate(lambda d: d["drains"].append({"minute": 1, "amount": "2"}))
        mutate(lambda d: d["drains"][0].update(minute=11))
        mutate(lambda d: d["committed_inflows"][0].update(minute=-1))
        mutate(lambda d: d["transitions"][0].update(to="missing"))
        mutate(lambda d: d["transitions"][0].update(**{"from": "a", "to": "a"}))
        mutate(lambda d: d["transitions"][0].update(to="START"))
        mutate(lambda d: d["transitions"].append(copy.deepcopy(d["transitions"][0])))
        mutate(
            lambda d: d["committed_inflows"].append(
                {"event_id": "e0", "minute": 3, "amount": "1", "source_ref": "s"}
            )
        )
        mutate(
            lambda d: d["committed_inflows"].append(
                {"event_id": "x", "minute": 12, "amount": "1", "source_ref": "s"}
            )
        )
        mutate(
            lambda d: d["committed_inflows"].append(
                {"event_id": "x", "minute": 1, "amount": "1", "source_ref": ""}
            )
        )

    def test_input_is_not_mutated_and_array_order_is_canonical(self):
        data = {
            "flow_rate": "10",
            "horizon_minutes": 12,
            "batches": [
                {"id": "b", "volume": "5", "release_minute": 0, "deadline_minute": 12, "priority": 2},
                {"id": "a", "volume": "5", "release_minute": 0, "deadline_minute": 12, "priority": 1},
            ],
            "transitions": [
                {"from": "START", "to": "b", "flush_volume": "1", "flush_minutes": 0},
                {"from": "START", "to": "a", "flush_volume": "1", "flush_minutes": 0},
                {"from": "a", "to": "b", "flush_volume": "1", "flush_minutes": 0},
                {"from": "b", "to": "a", "flush_volume": "1", "flush_minutes": 0},
            ],
            "initial_waste": "0",
            "waste_capacity": "100",
            "drains": [{"minute": 4, "amount": "1"}, {"minute": 1, "amount": "1"}],
            "committed_inflows": [
                {"event_id": "z", "minute": 2, "amount": "1", "source_ref": "s"},
                {"event_id": "a", "minute": 2, "amount": "1", "source_ref": "s"},
            ],
        }
        snapshot = copy.deepcopy(data)
        first = plan_campaign(data)
        self.assertEqual(data, snapshot)

        reordered = copy.deepcopy(data)
        reordered["batches"].reverse()
        reordered["transitions"].reverse()
        reordered["drains"].reverse()
        reordered["committed_inflows"].reverse()
        second = plan_campaign(reordered)
        self.assertEqual(first["sequence"], second["sequence"])
        self.assertEqual(first["waste_ledger"], second["waste_ledger"])
        self.assertEqual(
            [(b["id"], b["status"]) for b in first["batches"]],
            [(b["id"], b["status"]) for b in second["batches"]],
        )


class ExhaustiveOracleTests(unittest.TestCase):
    @staticmethod
    def oracle(data):
        rate = Fraction(data["flow_rate"])
        horizon = data["horizon_minutes"]
        batches = data["batches"]
        by_id = {b["id"]: b for b in batches}
        duration = {
            b["id"]: ceil_div(Fraction(b["volume"]), rate) for b in batches
        }
        edges = {}
        for edge in data["transitions"]:
            flush = Fraction(edge["flush_volume"])
            edges[(edge["from"], edge["to"])] = (
                flush,
                edge["flush_minutes"] + ceil_div(flush, rate),
            )
        drains = {d["minute"]: Fraction(d["amount"]) for d in data["drains"]}
        inflows = {}
        for event in data["committed_inflows"]:
            inflows[event["minute"]] = inflows.get(event["minute"], Fraction(0)) + Fraction(
                event["amount"]
            )
        initial = Fraction(data["initial_waste"])
        capacity = Fraction(data["waste_capacity"])

        balance = initial
        baseline_conflict = balance > capacity
        for minute in sorted(set(drains) | set(inflows)):
            if minute in drains:
                balance -= min(drains[minute], balance)
            if minute in inflows:
                balance += inflows[minute]
            if balance > capacity:
                baseline_conflict = True
        if baseline_conflict:
            return None, "baseline_conflict"

        best = None
        ids = list(by_id)
        for size in range(len(ids) + 1):
            for subset in itertools.permutations(ids, size):
                current = 0
                flush_total = Fraction(0)
                completion_sum = 0
                flushes = []
                details = []
                feasible = True
                for position, bid in enumerate(subset):
                    source = "START" if position == 0 else subset[position - 1]
                    if (source, bid) not in edges:
                        feasible = False
                        break
                    flush, transition_duration = edges[(source, bid)]
                    batch = by_id[bid]
                    lower = max(current + transition_duration, batch["release_minute"])
                    latest = batch["deadline_minute"] - duration[bid]
                    chosen = None
                    for minute in range(lower, latest + 1):
                        balance = initial
                        flush_at = {}
                        for m, amount in flushes:
                            flush_at[m] = flush_at.get(m, Fraction(0)) + amount
                        for m in range(minute + 1):
                            if m in drains:
                                balance -= min(drains[m], balance)
                            if m in inflows:
                                balance += inflows[m]
                            if m in flush_at:
                                balance += flush_at[m]
                        if balance + flush <= capacity:
                            chosen = minute
                            break
                    if chosen is None:
                        feasible = False
                        break
                    flushes.append((chosen, flush))
                    flush_total += flush
                    completion = chosen + duration[bid]
                    completion_sum += completion
                    current = completion
                    details.append((bid, chosen, completion))
                if not feasible:
                    continue
                counts = [0] * 5
                for bid in subset:
                    counts[by_id[bid]["priority"] - 1] += 1
                candidate = (tuple(counts), flush_total, completion_sum, subset, details)
                if best is None:
                    best = candidate
                    continue
                if candidate[0] > best[0]:
                    best = candidate
                elif candidate[0] == best[0]:
                    if candidate[1] < best[1]:
                        best = candidate
                    elif candidate[1] == best[1]:
                        if candidate[2] < best[2]:
                            best = candidate
                        elif candidate[2] == best[2] and candidate[3] < best[3]:
                            best = candidate
        return best, "compliant"

    @staticmethod
    def random_case(rng, n):
        horizon = rng.randint(10, 24)
        ids = [f"b{i}" for i in range(n)]
        batches = []
        for bid in ids:
            release = rng.randint(0, max(0, horizon - 3))
            deadline = rng.randint(release + 1, horizon)
            if rng.random() < 0.35:
                deadline = min(horizon, release + rng.randint(1, 5))
            batches.append(
                {
                    "id": bid,
                    "volume": str(rng.choice([1, 5, 10, 11, 20, 50])),
                    "release_minute": release,
                    "deadline_minute": deadline,
                    "priority": rng.randint(1, 5),
                }
            )
        transitions = []
        for bid in ids:
            if rng.random() < 0.78:
                transitions.append(
                    {
                        "from": "START",
                        "to": bid,
                        "flush_volume": str(rng.choice([0, 0, 1, 3, 5, 11])),
                        "flush_minutes": rng.randint(0, 3),
                    }
                )
        for a in ids:
            for b in ids:
                if a != b and rng.random() < 0.5:
                    transitions.append(
                        {
                            "from": a,
                            "to": b,
                            "flush_volume": str(rng.choice([0, 0, 1, 3, 5])),
                            "flush_minutes": rng.randint(0, 3),
                        }
                    )
        capacity = rng.choice([18, 35, 80, 1000])
        drains = [
            {"minute": minute, "amount": str(rng.randint(1, 18))}
            for minute in rng.sample(range(horizon + 1), rng.randint(0, min(4, horizon + 1)))
        ]
        inflows = []
        for k in range(rng.randint(0, 4)):
            inflows.append(
                {
                    "event_id": f"e{k}",
                    "minute": rng.randint(0, horizon),
                    "amount": str(rng.randint(1, 12)),
                    "source_ref": "oracle-source",
                }
            )
        return {
            "flow_rate": rng.choice(["1", "5", "10", "100"]),
            "horizon_minutes": horizon,
            "batches": batches,
            "transitions": transitions,
            "initial_waste": str(rng.randint(0, min(14, capacity))),
            "waste_capacity": str(capacity),
            "drains": drains,
            "committed_inflows": inflows,
        }

    def test_random_small_cases_match_subset_permutation_oracle(self):
        rng = random.Random(20261125)
        for iteration in range(140):
            data = self.random_case(rng, rng.randint(1, 6))
            oracle_result, environment = self.oracle(data)
            result = plan_campaign(copy.deepcopy(data))
            if environment == "baseline_conflict":
                self.assertEqual(result["environment_status"], "baseline_conflict", iteration)
                self.assertEqual(result["sequence"], [], iteration)
                continue
            self.assertEqual(result["environment_status"], "compliant", iteration)
            self.assertIsNotNone(oracle_result, iteration)
            counts, flush_total, completion_sum, sequence, details = oracle_result
            self.assertEqual(tuple(result["sequence"]), sequence, (iteration, data))
            self.assertEqual(tuple(result["priority_counts"]), counts, iteration)
            self.assertEqual(
                Fraction(result["total_flush_volume"]), flush_total, iteration
            )
            self.assertEqual(
                result["total_completion_minutes"], completion_sum, iteration
            )
            for row, (bid, start, completion) in zip(
                result["plan"]["batches"], details
            ):
                self.assertEqual(row["batch_id"], bid)
                self.assertEqual(row["start_minute"], start)
                self.assertEqual(row["complete_minute"], completion)


class Performance18Tests(unittest.TestCase):
    def test_eighteen_batches_use_subset_dp_not_factorial_search(self):
        n = 18
        rng = random.Random(9918)
        ids = [f"b{i:02d}" for i in range(n)]
        batches = [
            {
                "id": bid,
                "volume": "10",
                "release_minute": 0,
                "deadline_minute": 300,
                "priority": rng.randint(1, 5),
            }
            for bid in ids
        ]
        transitions = [
            {
                "from": "START",
                "to": bid,
                "flush_volume": str(i % 3),
                "flush_minutes": i % 2,
            }
            for i, bid in enumerate(ids)
        ]
        # Two outgoing edges per node still creates many interacting subsets,
        # but keeps the Held-Karp transition count bounded and deterministic.
        for i, source in enumerate(ids):
            for step in (1, 2):
                transitions.append(
                    {
                        "from": source,
                        "to": ids[(i + step) % n],
                        "flush_volume": str((i + step) % 3),
                        "flush_minutes": (i + step) % 2,
                    }
                )
        data = {
            "flow_rate": "10",
            "horizon_minutes": 300,
            "batches": batches,
            "transitions": transitions,
            "initial_waste": "0",
            "waste_capacity": "100000",
            "drains": [],
            "committed_inflows": [],
        }
        start = time.perf_counter()
        result = plan_campaign(data)
        elapsed = time.perf_counter() - start
        self.assertEqual(len(result["sequence"]), 18)
        self.assertLess(elapsed, 8.0)


if __name__ == "__main__":
    unittest.main()
