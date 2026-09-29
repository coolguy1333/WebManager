"""Container usage figures without making a page wait for the container runtime.

`docker stats` needs a second or more to take a reading (longer with many
containers). A page that called it directly made every visit, and every
2-second live refresh, wait that long while holding one of the web server's few
threads. Pages read the newest reading from here instead: one background
thread keeps it fresh while somebody is watching and goes quiet when nobody is.
"""

import threading
import time


class UsageSampler:
    def __init__(
        self,
        sample,
        *,
        background: bool = True,
        max_age: float = 30.0,
        watch_seconds: float = 20.0,
        pause: float = 0.5,
        failure_pause: float = 5.0,
        logger=None,
    ):
        """``sample()`` returns ``{container name: usage}``; it may be slow.

        ``background=False`` samples inside ``latest()`` instead (for tests).
        """
        self._sample = sample
        self._background = background
        self._max_age = max_age
        self._watch_seconds = watch_seconds
        self._pause = pause
        self._failure_pause = failure_pause
        self._logger = logger
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._readings: dict[str, dict] = {}
        self._taken_at: float | None = None
        self._watched_until = 0.0

    def latest(self) -> dict[str, dict]:
        """The newest readings by container name. Never waits for the runtime;
        empty until the first reading arrives and again if they get too old to
        be believed. Calling it also asks for fresh readings for a while."""
        if self._background:
            self._watch()
        else:
            self._take()
        with self._lock:
            if self._taken_at is None or time.monotonic() - self._taken_at > self._max_age:
                return {}
            return dict(self._readings)

    # -- internals -----------------------------------------------------------
    def _watch(self):
        self._watched_until = time.monotonic() + self._watch_seconds
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="webmanager-usage-sampler", daemon=True
                )
                self._thread.start()
        self._wake.set()

    def _run(self):
        while True:
            self._wake.wait()
            self._wake.clear()
            while time.monotonic() < self._watched_until:
                time.sleep(self._pause if self._take() else self._failure_pause)

    def _take(self) -> bool:
        try:
            readings = self._sample()
        except Exception as exc:  # noqa: BLE001 - a broken runtime must not stop the sampler
            if self._logger is not None:
                self._logger.debug("Container usage sample failed: %s", exc)
            return False
        with self._lock:
            self._readings = readings
            self._taken_at = time.monotonic()
        return True
