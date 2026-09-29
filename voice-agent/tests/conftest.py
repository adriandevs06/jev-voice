"""Tests never write to the real timeline log (.voice.log) -- it is what the
next "it did nothing" gets diagnosed from."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def _private_voice_log(monkeypatch, tmp_path):
    import server
    monkeypatch.setattr(server, "VOICE_LOG", tmp_path / "voice.log")
