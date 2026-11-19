"""Each process imports a different subset of modules. Load each entrypoint in a fresh
interpreter so a model that only works thanks to another module's import is caught."""

import subprocess
import sys

import pytest

CHECK = (
    "import importlib, sys; "
    "importlib.import_module(sys.argv[1]); "
    "from sqlalchemy.orm import configure_mappers; configure_mappers()"
)


@pytest.mark.parametrize("module", ["travelrag.worker.tasks", "travelrag.api.main"])
def test_entrypoint_imports_and_mappers_configure(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", CHECK, module], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
