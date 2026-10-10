"""
Pytest conftest: register the hyphenated package under its importable name.

health-connect-bridge cannot be imported via `import` syntax due to the hyphen,
but Python can import it via importlib.  We register it in sys.modules so that
relative imports (from ..module import ...) within the tests/ subpackage work.
"""
import importlib
import sys
from pathlib import Path

_SERVICES_DIR = Path(__file__).parent.parent  # services/

if str(_SERVICES_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVICES_DIR))

_pkg = importlib.import_module("health-connect-bridge")
# Register subpackage too so pytest can find tests/
_tests = importlib.import_module("health-connect-bridge.tests")
