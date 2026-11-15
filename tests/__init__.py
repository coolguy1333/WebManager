"""Tests never touch the project's own ``instance`` folder: apps created
without an explicit data folder use a throwaway one for this test run."""

import atexit
import os
import shutil
import tempfile

_data = tempfile.mkdtemp(prefix="webmanager-tests-")
os.environ["WEBMANAGER_DATA_DIR"] = _data
atexit.register(shutil.rmtree, _data, ignore_errors=True)

