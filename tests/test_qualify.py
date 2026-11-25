from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pipeline_interface import qualify  # noqa: E402


def interval_pairs(intervals):
    return [(item["start"], item["end"]) for item in intervals]


class QualifyAcceptanceTests(unittest.TestCase):
    def acceptance_one_data(self, terminal_c_end=("0.55", "0.05")):
        # component/uncertainty pairs encode [0.40,0.50],
        # [0.45,0.55] and [0.50,0.60].
        end_component, end_uncertainty = terminal_c_end
        return {
            "loss_permille": "0",
            "batches": [{"id": "a", "nominal_volume": "100"}],
            "meters": [
                {"id": "m0", "position_km": "0", "cumulative_volume": "100"},
                {"id": "m1", "position_km": "1", "cumulative_volume": "100"},
            ],
            "specs": [
                {
                    "batch_id": "a",
                    "min_component": "0.4",
                    "max_component": "0.6",
                    "max_gap": "50",
                }
            ],
            "samples": [
                {
                    "sample_id": "s0",
                    "position": "0",
                    "component": "0.45",
                    "uncertainty": "0.05",
                },
                {
                    "sample_id": "s1",
                    "position": "50",
                    "component": "0.5",
                    "uncertainty": "0.05",
                },
                {
                    "sample_id": "s2",
                    "position": "100",
                    "component": end_component,
                    "uncertainty": end_uncertainty,
                },
            ],
            "withdrawals": [],
        }

    def test_conservative_envelope_releases_full_interval(self):
        result = qualify(self.acceptance_one_data())
        batch = result["batches"][0]
        self.assertEqual(interval_pairs(batch["released_intervals"]), [("0", "100")])
        self.assertEqual(batch["held_intervals"], [])
        # The touching intervals also admit a constant 1/2 curve, so under the
        # stated closed-interval semantics both directions are feasible.
        self.assertEqual(
            batch["feasible_directions"], ["nondecreasing", "nonincreasing"]
        )

    def test_widened_terminal_interval_blocks_second_segment(self):
        data = self.acceptance_one_data(("0.6", "0.1"))  # [0.5, 0.7]
        batch = qualify(data)["batches"][0]
        self.assertEqual(interval_pairs(batch["released_intervals"]), [("0", "50")])
        self.assertEqual(interval_pairs(batch["held_intervals"]), [("50", "100")])
        assessment = batch["segment_assessments"][1]
        self.assertFalse(assessment["releasable"])
        self.assertEqual(
            assessment["conservative_upper_envelope"], "7/10"
        )

    def test_common_direction_conservatism_and_global_conflict(self):
        data = self.acceptance_one_data()
        # Broad first interval makes the non-increasing envelope materially
        # wider on the first gap while both global directions remain feasible.
        data["samples"][0].update(component="0.6", uncertainty="0.2")  # [0.4,0.8]
        batch = qualify(data)["batches"][0]
        first_gap = batch["segment_assessments"][0]
        self.assertEqual(batch["feasible_directions"], ["nondecreasing", "nonincreasing"])
        self.assertEqual(first_gap["directions"]["nondecreasing"]["lower_envelope"], "2/5")
        self.assertEqual(first_gap["directions"]["nondecreasing"]["upper_envelope"], "11/20")
        self.assertTrue(first_gap["directions"]["nondecreasing"]["within_spec"])
        self.assertEqual(first_gap["directions"]["nonincreasing"]["lower_envelope"], "1/2")
        self.assertEqual(first_gap["directions"]["nonincreasing"]["upper_envelope"], "4/5")
        self.assertFalse(first_gap["directions"]["nonincreasing"]["within_spec"])
        self.assertFalse(first_gap["releasable"])
        # The second gap is safe under both directions.
        self.assertTrue(batch["segment_assessments"][1]["releasable"])
        self.assertEqual(interval_pairs(batch["released_intervals"]), [("50", "100")])
        self.assertEqual(interval_pairs(batch["held_intervals"]), [("0", "50")])

        conflict = copy.deepcopy(data)
        conflict["specs"][0]["min_component"] = "0"
        conflict["samples"] = [
            {"sample_id": "h", "position": "0", "component": "0.65", "uncertainty": "0.05"},
            {"sample_id": "m", "position": "50", "component": "0.35", "uncertainty": "0.05"},
            {"sample_id": "t", "position": "100", "component": "0.65", "uncertainty": "0.05"},
        ]
        conflict_batch = qualify(conflict)["batches"][0]
        self.assertEqual(conflict_batch["feasible_directions"], [])
        self.assertEqual(conflict_batch["conflict_reason"], "no_consistent_monotonic_direction")
        self.assertEqual(interval_pairs(conflict_batch["held_intervals"]), [("0", "100")])
        self.assertEqual(conflict_batch["released_intervals"], [])

    def test_shared_boundary_string_canonicalization_gap_and_undelivered(self):
        data = {
            "loss_permille": "0",
            "batches": [
                {"id": "a", "nominal_volume": "60"},
                {"id": "b", "nominal_volume": "50"},
            ],
            # Terminal is 100: A delivered 60, B delivered 40 and short 10.
            "meters": [
                {"id": "m0", "position_km": "0", "cumulative_volume": "110"},
                {"id": "m1", "position_km": "1", "cumulative_volume": "100"},
            ],
            "specs": [
                {"batch_id": "a", "min_component": "0", "max_component": "1", "max_gap": "40"},
                {"batch_id": "b", "min_component": "0", "max_component": "1", "max_gap": "20"},
            ],
            "samples": [
                {"sample_id": "a20", "position": "20", "component": "0.5", "uncertainty": "0"},
                {"sample_id": "boundary-1", "position": "60.0", "component": "0.5", "uncertainty": "0"},
                {"sample_id": "boundary-2", "position": "60.00", "component": "0.5", "uncertainty": "0"},
                {"sample_id": "b70", "position": "70", "component": "0.5", "uncertainty": "0"},
                {"sample_id": "b100", "position": "100", "component": "0.5", "uncertainty": "0"},
            ],
            "withdrawals": [],
        }
        result = qualify(data)
        a, b = result["batches"]
        self.assertEqual(interval_pairs(a["held_intervals"]), [("0", "20")])
        self.assertEqual(interval_pairs(a["released_intervals"]), [("20", "60")])
        self.assertEqual([x["position"] for x in a["position_intervals"]], ["20", "60"])
        self.assertEqual([x["position"] for x in b["position_intervals"]], ["60", "70", "100"])
        self.assertEqual(interval_pairs(b["released_intervals"]), [("60", "70")])
        self.assertEqual(interval_pairs(b["held_intervals"]), [("70", "100")])
        boundary = next(x for x in b["position_intervals"] if x["position"] == "60")
        self.assertEqual(boundary["sample_ids"], ["boundary-1", "boundary-2"])

        # A fully undelivered trailing batch.
        data["meters"][1]["cumulative_volume"] = "60"
        data["samples"] = [s for s in data["samples"] if s["position"] in ("20", "60.0", "60.00")]
        result = qualify(data)
        self.assertEqual(result["terminal_volume"], "60")
        self.assertEqual(result["batches"][1]["status"], "not_delivered")
        self.assertEqual(result["batches"][1]["released_intervals"], [])
        self.assertEqual(result["batches"][1]["held_intervals"], [])

    def test_co_located_empty_intersection_conflicts_batch(self):
        data = self.acceptance_one_data()
        data["samples"] = [
            {"sample_id": "s0", "position": "0", "component": "0.5", "uncertainty": "0"},
            {"sample_id": "bad-a", "position": "50.0", "component": "0.4", "uncertainty": "0.01"},
            {"sample_id": "bad-b", "position": "50", "component": "0.6", "uncertainty": "0.01"},
            {"sample_id": "s2", "position": "100", "component": "0.5", "uncertainty": "0"},
        ]
        batch = qualify(data)["batches"][0]
        self.assertTrue(batch["evidence_conflict"])
        self.assertEqual(batch["conflict_reason"], "empty_error_intersection")
        self.assertTrue(batch["position_intervals"][1]["is_empty"])
        self.assertEqual(interval_pairs(batch["held_intervals"]), [("0", "100")])

    def test_sample_outside_terminal_or_in_shortfall_is_rejected(self):
        data = self.acceptance_one_data()
        data["samples"][-1]["position"] = "100.0001"
        with self.assertRaises(ValueError):
            qualify(data)

        shortfall = self.acceptance_one_data()
        shortfall["batches"][0]["nominal_volume"] = "120"
        shortfall["meters"][0]["cumulative_volume"] = "120"
        shortfall["samples"][-1]["position"] = "110"
        with self.assertRaises(ValueError):
            qualify(shortfall)

    def test_split_releases_atomic_withdrawals_and_input_unchanged(self):
        data = {
            "loss_permille": "0",
            "batches": [{"id": "a", "nominal_volume": "100"}],
            "meters": [
                {"id": "m0", "position_km": "0", "cumulative_volume": "100"},
                {"id": "m1", "position_km": "1", "cumulative_volume": "100"},
            ],
            "specs": [
                {"batch_id": "a", "min_component": "0.4", "max_component": "0.6", "max_gap": "30"}
            ],
            "samples": [
                {"sample_id": "s0", "position": "0", "component": "0.5", "uncertainty": "0"},
                {"sample_id": "s20", "position": "20", "component": "0.5", "uncertainty": "0"},
                {"sample_id": "s80", "position": "80", "component": "0.5", "uncertainty": "0"},
                {"sample_id": "s100", "position": "100", "component": "0.5", "uncertainty": "0"},
            ],
            "withdrawals": [
                {"withdrawal_id": "w1", "batch_id": "a", "volume": "30"},
                {"withdrawal_id": "w2", "batch_id": "a", "volume": "15"},
                {"withdrawal_id": "w3", "batch_id": "a", "volume": "5"},
            ],
        }
        before = copy.deepcopy(data)
        result = qualify(data)
        self.assertEqual(data, before)

        batch = result["batches"][0]
        self.assertEqual(
            interval_pairs(batch["released_intervals"]), [("0", "20"), ("80", "100")]
        )
        w1, w2, w3 = result["withdrawals"]
        self.assertEqual(w1["status"], "fulfilled")
        self.assertEqual(
            [(s["start"], s["end"], s["volume"]) for s in w1["segments"]],
            [("0", "20", "20"), ("80", "90", "10")],
        )
        self.assertEqual(w2["status"], "unfulfilled")
        self.assertEqual(w2["segments"], [])
        self.assertEqual(w3["status"], "fulfilled")
        self.assertEqual(
            [(s["start"], s["end"], s["volume"]) for s in w3["segments"]],
            [("90", "95", "5")],
        )

        intervals = batch["released_intervals"]
        self.assertEqual((intervals[0]["allocated_volume"], intervals[0]["remaining_volume"]), ("20", "0"))
        self.assertEqual((intervals[1]["allocated_volume"], intervals[1]["remaining_volume"]), ("15", "5"))

    def test_reference_identity_and_amount_errors_reject_all(self):
        base = {
            "loss_permille": "0",
            "batches": [{"id": "a", "nominal_volume": "10"}],
            "meters": [
                {"id": "m0", "position_km": "0", "cumulative_volume": "10"},
                {"id": "m1", "position_km": "1", "cumulative_volume": "10"},
            ],
            "specs": [
                {"batch_id": "a", "min_component": "0", "max_component": "1", "max_gap": "1"}
            ],
            "samples": [
                {"sample_id": "s", "position": "0", "component": "0.5", "uncertainty": "0"}
            ],
            "withdrawals": [],
        }

        unknown = copy.deepcopy(base)
        unknown["withdrawals"] = [{"withdrawal_id": "w", "batch_id": "missing", "volume": "1"}]
        with self.assertRaises(ValueError):
            qualify(unknown)

        duplicate = copy.deepcopy(base)
        duplicate["withdrawals"] = [
            {"withdrawal_id": "w", "batch_id": "a", "volume": "1"},
            {"withdrawal_id": "w", "batch_id": "a", "volume": "1"},
        ]
        with self.assertRaises(ValueError):
            qualify(duplicate)

        for bad_volume in ("0", "-1"):
            bad = copy.deepcopy(base)
            bad["withdrawals"] = [
                {"withdrawal_id": "w", "batch_id": "a", "volume": bad_volume}
            ]
            with self.assertRaises(ValueError):
                qualify(bad)

    def test_spec_must_cover_batch_exactly_once(self):
        data = {
            "loss_permille": "0",
            "batches": [{"id": "a", "nominal_volume": "1"}],
            "meters": [
                {"id": "m0", "position_km": "0", "cumulative_volume": "1"},
                {"id": "m1", "position_km": "1", "cumulative_volume": "1"},
            ],
            "specs": [],
            "samples": [
                {"sample_id": "s", "position": "0", "component": "0.5", "uncertainty": "0"}
            ],
        }
        with self.assertRaises(ValueError):
            qualify(data)


if __name__ == "__main__":
    unittest.main()
