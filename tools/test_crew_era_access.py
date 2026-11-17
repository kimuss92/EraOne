"""crew era_access: the crew can SEE all of Era One — read-only, credentials never.

Pinned from 2026-11-06: Laura asked Tom and Nabar to review staged self-improvement
e46dc, and Nabar answered the change was "missing" — candidates live under runtime/,
which source.read refuses, so Era told the user to show the diff on the HUD herself.

* development.read returns a staged candidate (request, plan, diff, verification) by
  full id or unique prefix, and does not taint the turn;
* era.read / era.list reach logs/ and runtime/, with tail and grep for big logs;
* credentials (api_keys*, dotfiles, *.pem, *token*, voiceprints), links and escapes
  are refused — including the api_keys copy inside a candidate workspace;
* an era.read outside source scope taints the turn (quoted text cannot shape a stage);
* every bot holds the read grant, and none of these capabilities write.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crew import capabilities as caps
from crew import era_access
from crew.registry import load_specs

REPO = Path(__file__).resolve().parent.parent
DEV_ID = "e46dc718133843a3"


def _cap(name, args):
    return caps.CAPABILITIES[name].fn(None, args)


@pytest.fixture
def era(tmp_path, monkeypatch):
    root = tmp_path / "EraOne"
    job = root / "runtime" / "development" / DEV_ID
    (job / "workspace" / "config").mkdir(parents=True)
    (job / "job.json").write_text(
        json.dumps(
            {
                "id": DEV_ID,
                "request": "activate mic after a manual mute",
                "state": "awaiting_approval",
                "phase": "Verified; awaiting your approval",
                "plan": "Update app/era_live/mic_mute.py",
                "attempt": 1,
                "checks": ["ruff", "pytest"],
                "files": [
                    {
                        "path": "app/era_live/mic_mute.py",
                        "base": "a" * 64,
                        "candidate": "b" * 64,
                        "new": False,
                        "diff": "--- a\n+++ b\n-old line\n+new line api_key=sk-abcdefghijklmnopqrstuvwx\n",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (job / "verification.txt").write_text("apply OK: app/era_live/mic_mute.py", encoding="utf-8")
    (job / "workspace" / "config" / "api_keys.json").write_text('{"k": "v"}', encoding="utf-8")
    (root / "logs").mkdir()
    (root / "logs" / "era.log").write_text(
        "\n".join(f"line {i} {'crew' if i % 10 == 0 else 'other'}" for i in range(1, 501)),
        encoding="utf-8",
    )
    (root / "config").mkdir()
    (root / "config" / "api_keys.json").write_text("{}", encoding="utf-8")
    (root / "config" / "speaker_profile.json").write_text("{}", encoding="utf-8")
    (root / "config" / "voice.json").write_text('{"mode": "gemini"}', encoding="utf-8")
    (root / ".env").write_text("SECRET=1", encoding="utf-8")
    (root / "blob.bin").write_bytes(b"\x00\x01\x02" * 10)
    monkeypatch.setattr(era_access, "ROOT", root)
    monkeypatch.setattr(era_access, "DEV_DIR", root / "runtime" / "development")
    return root


class TestDevelopmentCandidates:
    def test_crew_can_read_the_staged_change_by_prefix(self, era):
        res = _cap("development.read", {"id": "e46dc"})
        assert res.success, res.message
        assert res.data["id"] == DEV_ID and res.data["state"] == "awaiting_approval"
        diff = res.data["files"][0]["diff"]
        assert "+new line" in diff and "sk-abcdefghijklmnopqrstuvwx" not in diff
        assert "apply OK" in res.data["verification_tail"]
        assert "approves nothing" in res.data["authority"]

    def test_list_shows_the_candidate(self, era):
        res = _cap("development.list", {})
        assert [c["id"] for c in res.data["candidates"]] == [DEV_ID]

    @pytest.mark.parametrize("bad", ["", "zz", "../x", "e4", "E46DC/../../"])
    def test_bad_ids_refuse(self, era, bad):
        assert _cap("development.read", {"id": bad}).error_type == "EraAccessRefused"

    def test_ambiguous_prefix_refuses(self, era):
        other = era / "runtime" / "development" / "e46dc000000000ff"
        other.mkdir()
        (other / "job.json").write_text(json.dumps({"id": other.name, "files": []}))
        res = _cap("development.read", {"id": "e46dc"})
        assert not res.success and "matches 2" in res.message

    def test_reading_a_candidate_does_not_taint(self, era):
        from crew.coordination import CURRENT_WORK, TeamWork

        work = TeamWork("w1", "nabar", "review e46dc")
        reset = CURRENT_WORK.set(work)
        try:
            assert _cap("development.read", {"id": DEV_ID}).success
        finally:
            CURRENT_WORK.reset(reset)
        assert work.tainted_by == set()


class TestEraRead:
    def test_reads_logs_with_tail_and_grep(self, era):
        res = _cap("era.read", {"path": "logs/era.log", "tail": 3, "grep": "crew"})
        assert res.success
        assert res.data["content"].splitlines()[-1].endswith("line 500 crew")
        assert len(res.data["content"].splitlines()) == 3

    def test_reads_from_a_start_line(self, era):
        res = _cap("era.read", {"path": "logs/era.log", "start_line": 10, "limit": 2})
        assert res.data["content"].splitlines() == ["10: line 10 crew", "11: line 11 other"]

    def test_lists_folders_without_credentials(self, era):
        names = [e["name"] for e in _cap("era.list", {"path": "config"}).data["entries"]]
        assert names == ["voice.json"]
        root_names = [e["name"] for e in _cap("era.list", {}).data["entries"]]
        assert ".env" not in root_names and "logs/" in root_names

    @pytest.mark.parametrize(
        "path",
        [
            "config/api_keys.json",
            "config/speaker_profile.json",
            ".env",
            ".git/config",
            f"runtime/development/{DEV_ID}/workspace/config/api_keys.json",
            "../outside.txt",
            "C:/Windows/win.ini",
            "logs\\era.log",
            "node_modules/x.js",
        ],
    )
    def test_credentials_and_escapes_refuse(self, era, path):
        assert _cap("era.read", {"path": path}).error_type == "EraAccessRefused"

    def test_binary_refuses(self, era):
        assert _cap("era.read", {"path": "blob.bin"}).error_type == "BinaryFile"

    def test_reading_logs_taints_the_turn(self, era):
        from crew.coordination import CURRENT_WORK, TeamWork

        work = TeamWork("w2", "cody", "why")
        reset = CURRENT_WORK.set(work)
        try:
            assert _cap("era.read", {"path": "logs/era.log", "tail": 1}).success
        finally:
            CURRENT_WORK.reset(reset)
        assert work.tainted_by == {"era.read"}


class TestGrant:
    def test_every_bot_can_see_era_one(self):
        specs = load_specs(REPO / "config" / "crew").specs
        assert specs, "crew failed to load"
        for name, spec in specs.items():
            for cap in ("era.list", "era.read", "development.list", "development.read"):
                assert cap in spec.capabilities, f"{name} lacks {cap}"

    def test_access_guidance_reaches_the_bot_prompt(self):
        from crew.runtime import _system

        spec = load_specs(REPO / "config" / "crew").specs["nabar"]
        assert "development.read" in _system(spec) and "never answer" in _system(spec)

    def test_no_era_capability_writes(self):
        names = {n for n in caps.CAPABILITIES if n.startswith(("era.", "development."))}
        assert names == {"era.list", "era.read", "development.list", "development.read"}
