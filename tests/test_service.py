import unittest
from pipeline_interface import process


class PipelineTests(unittest.TestCase):
    def base(self):
        return {"loss_permille":"20","batches":[{"id":"a","nominal_volume":"100"},{"id":"b","nominal_volume":"50"}],"meters":[{"id":"m0","position_km":"0","cumulative_volume":"150"},{"id":"m1","position_km":"10","cumulative_volume":"148"},{"id":"m2","position_km":"20","cumulative_volume":"140"}]}

    def test_allocates_terminal_volume_in_batch_order(self):
        result = process(self.base())
        self.assertEqual(result["batches"][1]["delivered_volume"], "40")
        self.assertEqual(result["interfaces"][0]["delivered_position"], "100")

    def test_flags_segment_loss(self):
        self.assertEqual([x["status"] for x in process(self.base())["segments"]], ["within_limit", "excess_loss"])

    def test_exact_boundary_without_second_batch_interface(self):
        data=self.base(); data["meters"][-1]["cumulative_volume"]="100"
        self.assertEqual(len(process(data)["interfaces"]), 1)

    def test_first_meter_must_tie(self):
        data=self.base(); data["meters"][0]["cumulative_volume"]="149"
        with self.assertRaises(ValueError): process(data)

    def test_cumulative_cannot_increase(self):
        data=self.base(); data["meters"][1]["cumulative_volume"]="151"
        with self.assertRaises(ValueError): process(data)

    def test_loss_limit_is_bounded(self):
        data=self.base(); data["loss_permille"]="1001"
        with self.assertRaises(ValueError): process(data)


if __name__ == "__main__": unittest.main()

