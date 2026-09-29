import unittest
from unittest.mock import patch

from webmanager import system_metrics


class CpuPercentTests(unittest.TestCase):
    def setUp(self):
        saved = dict(system_metrics._cpu_last)
        self.addCleanup(system_metrics._cpu_last.update, saved)
        system_metrics._cpu_last.update(at=0.0, times=None, percent=None)
        self.now = 1000.0
        self.slept = []

        def sleep(seconds):
            self.slept.append(seconds)
            self.now += seconds

        patchers = [
            patch.object(system_metrics.time, "monotonic", lambda: self.now),
            patch.object(system_metrics.time, "sleep", sleep),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def cpu(self, *readings):
        return patch.object(system_metrics, "_cpu_times", side_effect=list(readings))

    def test_only_the_first_call_waits_to_have_something_to_compare(self):
        with self.cpu((1000, 900), (1100, 950)):
            self.assertEqual(system_metrics.cpu_percent(), 50.0)
        self.assertEqual(self.slept, [0.2])

        self.now += 2.0  # the next live refresh
        with self.cpu((1300, 1000)):
            self.assertEqual(system_metrics.cpu_percent(), 75.0)  # busy 150 of the last 200
        self.assertEqual(self.slept, [0.2])

    def test_calls_in_quick_succession_share_one_measurement(self):
        with self.cpu((1000, 900), (1100, 950)):
            system_metrics.cpu_percent()
        self.now += 0.1
        with self.cpu((1150, 960)):
            self.assertEqual(system_metrics.cpu_percent(), 50.0)  # not a new, tiny window

    def test_a_long_gap_measures_afresh_instead_of_averaging_the_gap(self):
        with self.cpu((1000, 900), (1100, 950)):
            system_metrics.cpu_percent()
        self.now += 3600
        with self.cpu((900000, 800000), (900100, 800100)):
            self.assertEqual(system_metrics.cpu_percent(), 0.0)
        self.assertEqual(self.slept, [0.2, 0.2])

    def test_unreadable_host_stats_give_none(self):
        with patch.object(system_metrics, "_cpu_times", return_value=None):
            self.assertIsNone(system_metrics.cpu_percent())


if __name__ == "__main__":
    unittest.main()
