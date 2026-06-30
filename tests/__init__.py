import sys
import types
from pathlib import Path

_src = Path(__file__).parent.parent / "src"

# src/ is the automysqlbackup package (pyproject.toml: package-dir.automysqlbackup = "src").
# Register a package stub so `from automysqlbackup.xxx import ...` resolves to src/xxx.py.
_pkg = types.ModuleType("automysqlbackup")
_pkg.__path__ = [str(_src)]
_pkg.__package__ = "automysqlbackup"
sys.modules.setdefault("automysqlbackup", _pkg)
