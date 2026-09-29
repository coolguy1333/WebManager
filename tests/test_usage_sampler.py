"""Pages must never wait for `docker stats`: usage comes from a sampler."""

import threading
import time
import unittest

from webmanager.usage_sampler import UsageSampler


def wait_for(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


class UsageSamplerTests(unittest.TestCase):
    def test_without_a_thread_every_call_takes_a_reading(self):
        readings = iter([{"a": 1}, {"a": 2}])
        sampler = UsageSampler(lambda: next(readings), background=False)

        self.assertEqual(sampler.latest(), {"a": 1})
        self.assertEqual(sampler.latest(), {"a": 2})

    def test_a_slow_runtime_never_holds_up_the_caller(self):
        release = threading.Event()
        started = threading.Event()

        def slow_sample():
            started.set()
            release.wait(5)
            return {"web": {"cpu_percent": 3.0}}

        sampler = UsageSampler(slow_sample, pause=0.01)
        began = time.monotonic()
        self.assertEqual(sampler.latest(), {})  # nothing yet, and no waiting for it
        self.assertLess(time.monotonic() - began, 0.5)
        self.assertTrue(started.wait(3), "the background thread should start sampling")

        release.set()
        self.assertTrue(wait_for(lambda: sampler.latest() == {"web": {"cpu_percent": 3.0}}))

    def test_sampling_stops_when_nobody_is_watching(self):
        calls = []
        sampler = UsageSampler(
            lambda: calls.append(1) or {"x": {}}, watch_seconds=0.15, pause=0.02
        )
        sampler.latest()
        self.assertTrue(wait_for(lambda: len(calls) >= 2), "keeps sampling while watched")

        time.sleep(0.4)  # the watch has run out
        settled = len(calls)
        time.sleep(0.3)
        self.assertEqual(len(calls), settled)

        sampler.latest()  # somebody looks again
        self.assertTrue(wait_for(lambda: len(calls) > settled), "wakes up for a new viewer")

    def test_a_failing_sample_is_survived_and_reports_nothing(self):
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("docker went away")
            return {"web": {"cpu_percent": 1.0}}

        sampler = UsageSampler(flaky, pause=0.01, failure_pause=0.01)
        self.assertEqual(sampler.latest(), {})
        self.assertTrue(wait_for(lambda: sampler.latest() == {"web": {"cpu_percent": 1.0}}))

    def test_readings_that_are_too_old_are_not_shown(self):
        sampler = UsageSampler(lambda: {"web": {}}, background=False, max_age=0.05)
        self.assertEqual(sampler.latest(), {"web": {}})
        sampler._sample = lambda: (_ for _ in ()).throw(RuntimeError("down"))
        time.sleep(0.1)
        self.assertEqual(sampler.latest(), {})


if __name__ == "__main__":
    unittest.main()
