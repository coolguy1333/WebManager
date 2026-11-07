"""Traffic reports must cover the whole period, not just since logrotate last
started a new log (it does that every day)."""

import gzip
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from webmanager import analytics


def entry(days_ago, host="demo.example", client="203.0.113.5", uri="/", status=200, size=100):
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago, minutes=5)
    return json.dumps(
        {"time": moment.isoformat(), "host": host, "status": status, "bytes": size,
         "client": client, "uri": uri}
    )


class RotatedLogTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.log = Path(self.directory.name) / "access.log"
        analytics._cache.clear()

    def write(self, name, lines, compress=False, age_days=0):
        path = self.log.parent / name
        data = ("\n".join(lines) + "\n").encode()
        path.write_bytes(gzip.compress(data) if compress else data)
        moment = time.time() - age_days * 86400
        os.utime(path, (moment, moment))
        return path

    def report(self, days=30, hosts=("demo.example",)):
        return analytics.aggregate_analytics(self.log, {1: list(hosts)}, days)

    def test_requests_in_rotated_logs_are_counted(self):
        self.write("access.log", [entry(0)])
        self.write("access.log.1", [entry(1), entry(1)], age_days=0.5)
        self.write("access.log.2.gz", [entry(2)] * 3, compress=True, age_days=1.5)

        data = self.report()

        self.assertEqual(data["requests"], 6)
        series = dict(data["daily"])
        self.assertEqual(sum(1 for count in series.values() if count), 3, "three different days")

    def test_the_period_is_respected(self):
        self.write("access.log", [entry(0)])
        self.write("access.log.1", [entry(3)], age_days=2.9)
        self.write("access.log.2.gz", [entry(40)], compress=True, age_days=39.9)

        self.assertEqual(self.report(days=7)["requests"], 2)
        self.assertEqual(self.report(days=90)["requests"], 3)
        self.assertEqual(self.report(days=30)["requests"], 2)

    def test_logs_from_before_the_period_are_not_even_read(self):
        self.write("access.log", [entry(0)])
        old = self.write("access.log.5.gz", [entry(60)], compress=True, age_days=59)
        opened = []
        real = analytics._read_lines
        analytics._read_lines = lambda file, budget: (opened.append(file), real(file, budget))[1]
        self.addCleanup(setattr, analytics, "_read_lines", real)

        self.report(days=30)

        self.assertNotIn(old, opened)

    def test_a_broken_archive_does_not_break_the_report(self):
        self.write("access.log", [entry(0)])
        (self.log.parent / "access.log.2.gz").write_bytes(b"not gzip at all")
        self.write("access.log.3.gz", [entry(3)], compress=True, age_days=2.9)

        self.assertEqual(self.report()["requests"], 2)

    def test_other_sites_and_unrelated_files_are_ignored(self):
        self.write("access.log", [entry(0), entry(0, host="other.example")])
        (self.log.parent / "access.log.old").write_text(entry(1) + "\n")
        (self.log.parent / "error.log.1").write_text(entry(1) + "\n")

        self.assertEqual(self.report()["requests"], 1)

    def test_one_report_reads_a_bounded_amount(self):
        self.write("access.log", [entry(0)] * 50)
        self.write("access.log.1", [entry(1)] * 50, age_days=0.5)
        original = analytics.MAX_LOG_BYTES
        analytics.MAX_LOG_BYTES = len(entry(0)) * 60
        self.addCleanup(setattr, analytics, "MAX_LOG_BYTES", original)

        self.assertLessEqual(self.report()["requests"], 60)
        self.assertGreater(self.report()["requests"], 50, "today's log is read first, then older ones")

    def test_the_answer_is_reused_until_a_log_changes(self):
        self.write("access.log", [entry(0)])
        self.assertEqual(self.report()["requests"], 1)

        calls = []
        real = analytics._aggregate
        analytics._aggregate = lambda *args: (calls.append(1), real(*args))[1]
        self.addCleanup(setattr, analytics, "_aggregate", real)
        self.report()
        self.assertEqual(calls, [], "unchanged logs: no second read")

        with self.log.open("a") as handle:
            handle.write(entry(0) + "\n")
        self.assertEqual(self.report()["requests"], 2)

    def test_a_single_site_report_also_spans_rotations(self):
        self.write("access.log", [entry(0)])
        self.write("access.log.1", [entry(1, client="198.51.100.9")], age_days=0.5)

        data = analytics.site_analytics(self.log, "demo.example", days=30)

        self.assertEqual(data["requests"], 2)
        self.assertEqual(data["visitors"], 2)

    def test_the_health_checks_own_visits_are_not_traffic(self):
        probe = json.dumps({"time": datetime.now(timezone.utc).isoformat(), "host": "demo.example",
                            "status": 200, "bytes": 5, "client": "", "uri": "/", "agent": analytics.HEALTH_PROBE_AGENT})
        self.write("access.log", [entry(0), probe, probe])
        self.assertEqual(self.report()["requests"], 1)
        self.assertEqual(analytics.site_analytics(self.log, "demo.example")["requests"], 1)

    def test_it_is_fine_when_there_is_no_log_yet(self):
        self.assertEqual(self.report()["requests"], 0)


if __name__ == "__main__":
    unittest.main()
