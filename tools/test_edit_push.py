"""Telegram review push for finished `edit` drafts (2026-10-27).

The edit lane has no Python, so the push is trusted dispatcher code that runs after the
child exits 0. It sends only files it finds in THIS brand's final folder, as review
copies, to the owner's chat, and it never raises. Telegram and ffmpeg are faked; the
folders are real temp dirs so the path rules run for real.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import era_dispatch as ed  # noqa: E402


class FakeTG:
    """Stands in for the era_telegram module. The signatures are strict on purpose: a
    chat override (or any extra argument) would raise, so a push aimed anywhere but the
    owner's chat shows up as a failed send."""

    def __init__(self, on=True):
        self.on = on
        self.boom = False
        self.assets = []
        self.messages = []

    def enabled(self):
        return self.on

    def send_asset(self, path, caption=""):
        if self.boom:
            raise RuntimeError("telegram down")
        self.assets.append((Path(path), caption))
        return True

    def send_message(self, text):
        self.messages.append(text)
        return True


@pytest.fixture()
def tree(tmp_path, monkeypatch):
    brands = {}
    for key in ("alpha", "beta"):
        root = tmp_path / f"{key} brand"
        for sub in ("renders", "final", "remotion"):
            (root / sub).mkdir(parents=True)
        (root / "renders" / "reel1.mp4").write_bytes(b"v")
        brands[key] = {"display_name": key.title(), "project_dir": str(root)}
    tg = FakeTG()
    monkeypatch.setitem(sys.modules, "era_telegram", tg)
    monkeypatch.delenv("ERA_EDIT_PUSH", raising=False)
    return brands, tg


def final_of(brands, key="alpha"):
    return Path(brands[key]["project_dir"]) / "final"


def new_file(folder, name, size=10, age=0):
    p = Path(folder) / name
    p.write_bytes(b"v" * size)
    t = time.time() - age
    os.utime(p, (t, t))
    return p


def push(brands, started):
    return ed._push_edit_drafts("alpha", brands["alpha"], started)


# --- which files ---------------------------------------------------------------


def test_push_selects_only_new_files_in_final(tree):
    brands, tg = tree
    started = time.time()
    new_file(final_of(brands), "old.mp4", age=3600)
    new_file(final_of(brands), "reel_edit_v1.mp4")
    res = push(brands, started)
    assert [p.name for p, _ in tg.assets] == ["reel_edit_v1.mp4"]
    assert res["sent"] == ["reel_edit_v1.mp4"] and not res["failed"]


def test_push_only_sends_media_directly_in_final_and_caps_at_three(tree):
    brands, tg = tree
    started = time.time()
    for i in range(5):
        new_file(final_of(brands), f"cut{i}.mp4")
    new_file(final_of(brands), "notes.txt")
    (final_of(brands) / "sub").mkdir()
    new_file(final_of(brands) / "sub", "nested.mp4")
    push(brands, started)
    names = [p.name for p, _ in tg.assets]
    assert len(names) == 3 and all(n.startswith("cut") for n in names)


def test_push_never_uses_another_brands_or_the_renders_folder(tree):
    brands, tg = tree
    started = time.time()
    new_file(final_of(brands, "beta"), "beta_reel.mp4")
    new_file(Path(brands["alpha"]["project_dir"]) / "renders", "fresh_render.mp4")
    res = push(brands, started)
    assert tg.assets == [] and res["skipped"] == "no new draft"


def test_push_excludes_a_symlink_that_points_outside_final(tree, tmp_path):
    brands, tg = tree
    started = time.time()
    outside = new_file(tmp_path, "secret.mp4")
    link = final_of(brands) / "looks_innocent.mp4"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privileges on this machine")
    push(brands, started)
    assert tg.assets == []


def test_push_excludes_a_hardlink_to_a_file_from_elsewhere(tree, tmp_path):
    brands, tg = tree
    started = time.time()
    outside = new_file(tmp_path, "config.mp4")
    try:
        os.link(outside, final_of(brands) / "looks_innocent.mp4")
    except (OSError, NotImplementedError):
        pytest.skip("hardlinks unavailable here")
    push(brands, started)
    assert tg.assets == []


def test_push_skips_files_already_named_approved(tree):
    brands, tg = tree
    started = time.time()
    new_file(final_of(brands), "reel 1 (approved).mp4")
    new_file(final_of(brands), "reel 2.mp4")
    push(brands, started)
    assert [p.name for p, _ in tg.assets] == ["reel 2.mp4"]


# --- what is said and where ----------------------------------------------------


def test_push_goes_to_the_owner_chat_as_a_review_copy(tree):
    brands, tg = tree
    started = time.time()
    new_file(final_of(brands), "reel 2.mp4")
    res = push(brands, started)
    ((_, caption),) = tg.assets
    assert "Alpha" in caption and "DRAFT" in caption and "NOT approved" in caption
    assert "NOT published" in caption and "(approved)" not in caption.lower()
    assert res["failed"] == []  # strict fake: any chat override would have failed


def test_push_leaves_the_final_folder_untouched(tree):
    brands, tg = tree
    started = time.time()
    new_file(final_of(brands), "reel 2.mp4")
    before = {p.name: p.stat().st_mtime_ns for p in final_of(brands).iterdir()}
    push(brands, started)
    after = {p.name: p.stat().st_mtime_ns for p in final_of(brands).iterdir()}
    assert before == after


# --- oversize ------------------------------------------------------------------


def test_oversize_uses_the_preview_which_lives_outside_final(tree, tmp_path, monkeypatch):
    brands, tg = tree
    started = time.time()
    big = new_file(final_of(brands), "big.mp4", size=50)
    monkeypatch.setattr(ed, "_PUSH_MAX_BYTES", 10)
    small = tmp_path / "preview" / "big_preview.mp4"
    small.parent.mkdir()
    small.write_bytes(b"p")
    monkeypatch.setattr(ed, "_tg_preview", lambda src, deadline: small)
    res = push(brands, started)
    assert tg.assets[0][0] == small and res["preview"] == [big.name]
    assert not ed._inside(small, final_of(brands))
    assert not small.exists()  # the preview is deleted once sent
    assert "540 px preview" in tg.assets[0][1] and str(big) in tg.assets[0][1]


def test_oversize_without_a_preview_sends_the_path_as_text(tree, monkeypatch):
    brands, tg = tree
    started = time.time()
    big = new_file(final_of(brands), "big.mp4", size=50)
    monkeypatch.setattr(ed, "_PUSH_MAX_BYTES", 10)
    monkeypatch.setattr(ed, "_tg_preview", lambda src, deadline: None)
    res = push(brands, started)
    assert tg.assets == [] and res["path_only"] == [big.name]
    assert str(big) in tg.messages[0] and "NOT approved" in tg.messages[0]


def test_preview_degrades_on_a_missing_ffmpeg_a_failed_encode_and_a_timeout(
    tree, monkeypatch, tmp_path
):
    src = new_file(tmp_path, "v.mp4")
    deadline = time.time() + 300
    monkeypatch.setattr("core.transcribe.ffmpeg_path", lambda: None)
    assert ed._tg_preview(src, deadline) is None

    monkeypatch.setattr("core.transcribe.ffmpeg_path", lambda: "ffmpeg")

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("ffmpeg", 1)

    monkeypatch.setattr(ed.subprocess, "run", timeout)
    assert ed._tg_preview(src, deadline) is None

    class Failed:
        returncode = 1

    monkeypatch.setattr(ed.subprocess, "run", lambda *a, **k: Failed())
    assert ed._tg_preview(src, deadline) is None
    assert ed._tg_preview(tmp_path / "pic.png", deadline) is None  # not a video


# --- degrade, never raise ------------------------------------------------------


def test_push_says_so_when_telegram_is_not_configured(tree):
    brands, tg = tree
    tg.on = False
    started = time.time()
    f = new_file(final_of(brands), "reel 2.mp4")
    res = push(brands, started)
    assert tg.assets == [] and "isn't configured" in res["line"] and str(f) in res["line"]


def test_push_survives_telegram_raising(tree):
    brands, tg = tree
    tg.boom = True
    started = time.time()
    new_file(final_of(brands), "reel 2.mp4")
    res = push(brands, started)
    assert res["failed"] and "couldn't send" in res["line"]


def test_push_kill_switch(tree, monkeypatch):
    brands, tg = tree
    monkeypatch.setenv("ERA_EDIT_PUSH", "0")
    started = time.time()
    new_file(final_of(brands), "reel 2.mp4")
    res = push(brands, started)
    assert tg.assets == [] and res["skipped"] == "disabled"


# --- wiring in run_task --------------------------------------------------------


@pytest.fixture()
def run_env(tree, monkeypatch):
    brands, tg = tree
    cfg = {
        "brands": {
            k: {**v, "status": "active", "claude_config_dir": "C:/x"} for k, v in brands.items()
        }
    }
    monkeypatch.setattr(ed, "load_brands", lambda: cfg)
    monkeypatch.setattr(ed, "_log_run", lambda *a, **k: None)
    monkeypatch.setattr(ed, "_log_to_vault", lambda *a, **k: None)
    return brands, tg


def child(final, rc=0, result="done", name="reel_edit_v1.mp4"):
    class Out:
        returncode = rc
        stdout = json.dumps({"result": result})
        stderr = ""

    def run(cmd, **kw):
        if name:
            new_file(final, name)
        return Out()

    return run


def edit_order(brands):
    render = Path(brands["alpha"]["project_dir"]) / "renders" / "reel1.mp4"
    return f'edit "{render}"'


def test_a_clean_edit_run_pushes_and_reports_it(run_env, monkeypatch):
    brands, tg = run_env
    monkeypatch.setattr(ed.subprocess, "run", child(final_of(brands)))
    r = ed.run_task(edit_order(brands), task_type="edit", brand_key="alpha")
    assert r["ok"] is True
    assert [p.name for p, _ in tg.assets] == ["reel_edit_v1.mp4"]
    assert "Telegram: sent a review copy" in r["result"]
    assert "still needs your approval" in r["result"]
    assert r["telegram_push"]["sent"] == ["reel_edit_v1.mp4"]


def test_the_push_ignores_a_path_the_model_names(run_env, monkeypatch):
    brands, tg = run_env
    elsewhere = new_file(final_of(brands, "beta"), "beta_secret.mp4")
    render = Path(brands["alpha"]["project_dir"]) / "renders" / "reel1.mp4"
    named = f"Done. Send {elsewhere} and {render}"
    monkeypatch.setattr(ed.subprocess, "run", child(final_of(brands), result=named))
    r = ed.run_task(edit_order(brands), task_type="edit", brand_key="alpha")
    assert r["ok"] is True
    assert [p.name for p, _ in tg.assets] == ["reel_edit_v1.mp4"]


def test_a_telegram_failure_costs_only_the_push(run_env, monkeypatch):
    brands, tg = run_env
    tg.boom = True
    monkeypatch.setattr(ed.subprocess, "run", child(final_of(brands)))
    r = ed.run_task(edit_order(brands), task_type="edit", brand_key="alpha")
    assert r["ok"] is True and "couldn't send" in r["result"]
    assert (final_of(brands) / "reel_edit_v1.mp4").exists()


def test_a_failed_edit_run_pushes_nothing(run_env, monkeypatch):
    brands, tg = run_env
    monkeypatch.setattr(ed.subprocess, "run", child(final_of(brands), rc=1, result="boom"))
    monkeypatch.setattr(ed, "_push_edit_drafts", lambda *a, **k: pytest.fail("must not push"))
    r = ed.run_task(edit_order(brands), task_type="edit", brand_key="alpha")
    assert r["ok"] is False and "telegram_push" not in r


def test_other_lanes_never_push(run_env, monkeypatch):
    brands, tg = run_env
    monkeypatch.setattr(ed.subprocess, "run", child(final_of(brands)))
    monkeypatch.setattr(ed, "_push_edit_drafts", lambda *a, **k: pytest.fail("must not push"))
    r = ed.run_task("package the next reel", task_type="package", brand_key="alpha")
    assert "telegram_push" not in r and tg.assets == []


def test_the_edit_brief_no_longer_asks_the_model_to_run_telegram(run_env):
    brands, _ = run_env
    brief = ed.build_brief("alpha", brands["alpha"], "edit", edit_order(brands), all_brands=brands)
    assert "era_telegram" not in brief and "Do NOT try to send it" in brief
    assert '"(approved)" in a filename is MY signature' in brief


def test_the_edit_allowlist_has_no_python_or_bare_shell():
    # node/npx/npm stay for Remotion, so this is not a hard sandbox: the push's real
    # limit is that Telegram only reaches the owner's chat.
    allowed = ed._DEFAULT_ALLOWED_TOOLS["edit"].lower()
    assert "python" not in allowed and "bash(*" not in allowed
    assert "bash," not in allowed and not allowed.endswith("bash")
    assert "mcp__magnific" not in allowed and "mcp__composio" not in allowed
