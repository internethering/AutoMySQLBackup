import importlib.util
import sys
from pathlib import Path

_src = Path(__file__).parent.parent / "src"

# src/ is the automysqlbackup package (pyproject.toml: package-dir.automysqlbackup = "src").
# Load it under its real name with a proper module spec, so `from automysqlbackup.xxx
# import ...` and importlib.resources work without installing the package.
if "automysqlbackup" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "automysqlbackup", _src / "__init__.py", submodule_search_locations=[str(_src)],
    )
    _pkg = importlib.util.module_from_spec(_spec)
    sys.modules["automysqlbackup"] = _pkg
    _spec.loader.exec_module(_pkg)
