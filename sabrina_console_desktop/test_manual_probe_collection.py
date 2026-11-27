"""Pytest discovery of a command-line probe must work offline."""
from pathlib import Path
import runpy
import sys
import urllib.request

import pytest


DESKTOP = Path(__file__).resolve().parent
PROBES = [
    DESKTOP / "tools" / "test_interactions_and_gradio_chat.py",
    DESKTOP / "tools" / "test_oracle_collective_hub.py",
]
# These two local ML demos are excluded from the public source distribution.
for name in ("snn_test.py", "test_snntorch.py"):
    demo = DESKTOP.parent / name
    if demo.is_file():
        PROBES.append(demo)


@pytest.mark.parametrize("script", PROBES, ids=lambda path: path.name)
def test_manual_probe_import_is_inert(script, monkeypatch, capsys):
    def unexpected_io(*args, **kwargs):
        pytest.fail("Importing the probe attempted a file or network operation")

    monkeypatch.setitem(sys.modules, "torch", None)
    monkeypatch.setitem(sys.modules, "tools.oracle_providers", None)
    monkeypatch.setattr(urllib.request, "urlopen", unexpected_io)
    with monkeypatch.context() as scoped:
        scoped.setattr("builtins.open", unexpected_io)
        runpy.run_path(str(script))
    assert capsys.readouterr().out == ""
