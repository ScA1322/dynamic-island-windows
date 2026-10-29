import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Every test gets its own settings/cache/log folder."""
    monkeypatch.setenv("DYNAMIC_ISLAND_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    return tmp_path / "home"
