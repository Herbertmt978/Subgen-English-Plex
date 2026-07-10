"""
conftest.py — patches heavy ML/AV dependencies before any test module imports subgen.

The mock setup must happen here, at collection time, before subgen.py is imported.
subgen.py starts worker threads at import time; they're daemon threads and are harmless.
"""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Mock heavy dependencies that are not installed in CI
# ---------------------------------------------------------------------------
_MOCKED_MODULES = [
    "stable_whisper",
    "faster_whisper",
    "torch",
    "av",
    "ffmpeg",
    "watchdog",
    "watchdog.observers",
    "watchdog.observers.polling",
    "watchdog.events",
    "numpy",
]

for _mod in _MOCKED_MODULES:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

# Give the version mocks a usable string so /status doesn't blow up
sys.modules["stable_whisper"].__version__ = "1.0.0"
sys.modules["faster_whisper"].__version__ = "1.0.0"

# Ensure sub-attribute imports work
# e.g. `from stable_whisper import Segment`
sys.modules["stable_whisper"].Segment = MagicMock()

# Ensure watchdog attribute imports work
# e.g. `from watchdog.observers.polling import PollingObserver as Observer`
sys.modules["watchdog.observers.polling"].PollingObserver = MagicMock()
# e.g. `from watchdog.events import FileSystemEventHandler`
sys.modules["watchdog.events"].FileSystemEventHandler = object

# The project deliberately names its drop-in runtime `subgen_override.py` and
# bind-mounts it as `/subgen/subgen.py`. Load that shipped file under the module
# name used by upstream's regression suite.
_SOURCE = Path(__file__).resolve().parents[1] / "subgen_override.py"
_SPEC = importlib.util.spec_from_file_location("subgen", _SOURCE)
assert _SPEC is not None and _SPEC.loader is not None
_SUBGEN = importlib.util.module_from_spec(_SPEC)
sys.modules["subgen"] = _SUBGEN
_SPEC.loader.exec_module(_SUBGEN)
