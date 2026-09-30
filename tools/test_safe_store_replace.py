"""safe_write_json's final rename survives a target Windows reports as busy.

2026-09-26: a full suite run failed with WinError 5 on os.replace of a memory
file in a temp dir — another process (Defender / the indexer) had it open for a
moment. The same call writes Era's live memory stores.
"""

import json
import os

import pytest

from memory import safe_store


def test_a_briefly_locked_target_is_retried(tmp_path, monkeypatch):
    real = os.replace
    fails = {"left": 2}

    def flaky(src, dst):
        if fails["left"]:
            fails["left"] -= 1
            raise PermissionError(5, "Access is denied")
        return real(src, dst)

    monkeypatch.setattr(safe_store.os, "replace", flaky)
    monkeypatch.setattr(safe_store.time, "sleep", lambda _s: None)
    target = tmp_path / "state.json"
    safe_store.safe_write_json(target, {"ok": True})
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}
    assert fails["left"] == 0


def test_a_lock_that_never_clears_still_raises_and_leaves_no_temp(tmp_path, monkeypatch):
    def locked(src, dst):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(safe_store.os, "replace", locked)
    monkeypatch.setattr(safe_store.time, "sleep", lambda _s: None)
    with pytest.raises(PermissionError):
        safe_store.safe_write_json(tmp_path / "state.json", {"ok": True})
    assert list(tmp_path.glob("*.json")) == []


def test_other_errors_are_not_retried(tmp_path, monkeypatch):
    calls = []

    def missing(src, dst):
        calls.append(src)
        raise FileNotFoundError(src)

    monkeypatch.setattr(safe_store.os, "replace", missing)
    with pytest.raises(FileNotFoundError):
        safe_store.safe_write_json(tmp_path / "state.json", {"ok": True})
    assert len(calls) == 1
