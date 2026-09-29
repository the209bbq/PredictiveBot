"""Keep the test suite out of the repo state/ and logs/ directories."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_pmbot_state(tmp_path, monkeypatch):
    monkeypatch.setenv("PMBOT_STATE_DIR", str(tmp_path / "pmbot-state"))
    yield
