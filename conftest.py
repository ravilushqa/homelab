"""
Root conftest: make health-connect-bridge (hyphenated) importable as a package
so relative imports inside tests/ work under pytest.

The directory name contains a hyphen which is invalid as a Python identifier,
so we pre-load all submodules and tests via importlib and register them in
sys.modules under their dotted names.  Pytest then finds pre-loaded modules
instead of trying to compute package names from hyphenated paths.
"""
import importlib
import sys
from pathlib import Path

_SERVICES = Path(__file__).parent / "services"
if str(_SERVICES) not in sys.path:
    sys.path.insert(0, str(_SERVICES))

# Load the package and all test submodules so pytest finds them in sys.modules
_PKG = "health-connect-bridge"
_TEST_MODULES = [
    "auth",
    "cli",
    "identity",
    "ingest",
    "nutrition_api",
    "nutrition_auth",
    "nutrition_cli",
    "nutrition_queue",
    "persistence",
    "privacy_gate",
    "rate_limit",
    "regressions",
    "routing",
    "security",
    "validation",
    "nutrition_e2e",
    "nutrition_installer",
]

importlib.import_module(_PKG)
importlib.import_module(f"{_PKG}.tests")
importlib.import_module(f"{_PKG}.tests.fixtures")
importlib.import_module(f"{_PKG}.tests.helpers")
for _m in _TEST_MODULES:
    try:
        importlib.import_module(f"{_PKG}.tests.test_{_m}")
    except Exception:
        pass
