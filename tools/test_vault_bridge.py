"""core/vault_bridge.py — the safe door into the vault.

What these tests pin, and why each one is silent-or-destructive if it regresses:

  * NO DELETE and NO SELF-APPROVAL — the action surface is exactly read/search/create/
    append/replace; `replace` only ever applies a plan that was staged.
  * THE WRITE CONTRACT — a lock around read-modify-write (concurrent appenders lose no
    line), atomic replace, and a read-back that reports a sync-clobbered write as a
    FAILURE, never "done".
  * STAGED REPLACE — single-use plan, hash-checked at apply, forged ids refused.
  * ISOLATION — a scoped bot cannot read another brand's notes and fails closed when
    the brand catalog is unreadable; it can only write where its zones allow.
  * DEGRADE, NEVER RAISE — bad path, bad bytes, missing vault: a ToolResult, always.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from core import cm_vault, vault_bridge
from core.vault_bridge import Caller

BRANDS = {
    "delta_cuisine": ["delta", "delta cuisine", "dc"],
    "ceram_quartz": ["ceram", "quartz", "ceram quartz"],
}
REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "era_brain"
    for folder in (
        "00 - Inbox",
        "01 - Daily Notes",
        "02 - Delta Cuisine",
        "03 - Ceram Quartz",
        "04 - Global",
        "05 - Archive",
        "06 - Resources",
    ):
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "02 - Delta Cuisine" / "Delta_Social_Strategy.md").write_text(
        "---\nbrand: delta_cuisine\ntype: reference\nstatus: active\n---\n"
        "# Strategy\n\n## Cadence\n\nFive reels a week.\n\n## Tone\n\nWarm.\n",
        encoding="utf-8",
    )
    (root / "03 - Ceram Quartz" / "Ceram_Pricing.md").write_text(
        "---\nbrand: ceram_quartz\ntype: reference\n---\n# Pricing\n\nSlabs start at a secret.\n",
        encoding="utf-8",
    )
    (root / "04 - Global" / "House_Rules.md").write_text(
        "---\ntype: reference\n---\n# Rules\n\nAlways cite the showroom address.\n",
        encoding="utf-8",
    )
    (root / "00 - Inbox" / "Scratch.md").write_text("# Scratch\n\nidea\n", encoding="utf-8")
    monkeypatch.setattr(cm_vault, "VAULT_DIR", root)
    monkeypatch.setenv("ERA_VAULT_VERIFY_SETTLE_S", "0")
    import era_dispatch as ed

    monkeypatch.setattr(
        ed,
        "resolve_brand",
        lambda name: next(
            (
                k
                for k, al in BRANDS.items()
                if name.strip().lower() in al or name.strip().lower() == k
            ),
            None,
        ),
    )
    monkeypatch.setattr(vault_bridge, "_brand_catalog", lambda: dict(BRANDS))
    return root


DELTA_BOT = Caller(
    actor="maya", brands=frozenset({"delta_cuisine"}), write_zones=frozenset({"inbox"})
)


def run(**kw):
    caller = kw.pop("caller", None)
    return vault_bridge.run(kw, caller=caller)


def stage_and_apply(path, content, section="", caller=None):
    plan_id, _, _ = vault_bridge.stage_replace(
        caller or Caller(), {"path": path, "content": content, "section": section}
    )
    return vault_bridge.run({"action": "replace", "_approved_plan": {"plan_id": plan_id}})


# ── the surface ─────────────────────────────────────────────────────────────
class TestSurface:
    def test_exactly_five_actions_and_no_delete(self):
        assert vault_bridge.ACTIONS == ("read", "search", "create", "append", "replace")
        assert vault_bridge.UNGATED_ACTIONS | vault_bridge.GATED_ACTIONS == set(
            vault_bridge.ACTIONS
        )
        for word in ("delete", "remove", "confirm", "reject", "approve"):
            assert word not in vault_bridge.ACTIONS

    @pytest.mark.parametrize("action", ["delete", "remove", "confirm", "reject", "", "rm"])
    def test_unknown_actions_fail_without_raising(self, vault, action):
        res = run(action=action)
        assert not res.success and res.error_type == "UnknownAction"

    def test_replace_without_a_staged_plan_is_refused(self, vault):
        res = run(action="replace", path="02 - Delta Cuisine/Delta_Social_Strategy.md", content="x")
        assert not res.success and res.error_type == "NotStaged"
        text = (vault / "02 - Delta Cuisine" / "Delta_Social_Strategy.md").read_text("utf-8")
        assert "Five reels" in text


# ── read ────────────────────────────────────────────────────────────────────
class TestRead:
    def test_read_by_path_and_by_loose_title(self, vault):
        a = run(action="read", path="02 - Delta Cuisine/Delta_Social_Strategy.md")
        b = run(action="read", title="delta social strategy", brand="delta")
        assert a.success and b.success
        assert "Five reels a week." in a.message
        assert a.data["sha256"] == b.data["sha256"]
        assert a.data["frontmatter"]["brand"] == "delta_cuisine"

    def test_missing_note(self, vault):
        res = run(action="read", title="Nope")
        assert not res.success and res.error_type == "NoteNotFound"

    @pytest.mark.parametrize(
        "bad",
        [
            "../outside.md",
            "02 - Delta Cuisine/../../x.md",
            "/etc/passwd",
            "C:/Windows/win.ini",
            ".obsidian/workspace.md",
            "\\\\server\\share\\x.md",
        ],
    )
    def test_path_traversal_refused(self, vault, bad):
        res = run(action="read", path=bad)
        assert not res.success and res.error_type in ("PathRefused", "NoteNotFound")

    def test_symlink_escape_refused(self, vault, tmp_path):
        outside = tmp_path / "secret.md"
        outside.write_text("top secret", encoding="utf-8")
        link = vault / "04 - Global" / "Link.md"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks need privilege on this machine")
        res = run(action="read", path="04 - Global/Link.md")
        assert not res.success and res.error_type == "PathRefused"

    def test_regenerated_note_refused(self, vault):
        (vault / "02 - Delta Cuisine" / "Era_Run_Log_delta_cuisine.md").write_text(
            "# log\n", encoding="utf-8"
        )
        res = run(action="read", path="02 - Delta Cuisine/Era_Run_Log_delta_cuisine.md")
        assert not res.success and res.error_type == "RegeneratedNote"

    def test_malformed_frontmatter_still_reads(self, vault):
        (vault / "04 - Global" / "Broken.md").write_text(
            "---\nthis: is: not: yaml\n\x00junk\n# no closing fence\nbody text\n", encoding="utf-8"
        )
        res = run(action="read", path="04 - Global/Broken.md")
        assert res.success and "body text" in res.message

    def test_non_utf8_note_degrades(self, vault):
        (vault / "04 - Global" / "Latin.md").write_bytes(b"# t\n\xe9\xe8 not utf8 \xff\xfe")
        res = run(action="read", path="04 - Global/Latin.md")
        assert not res.success and res.error_type == "NotUtf8"


# ── search ──────────────────────────────────────────────────────────────────
class TestSearch:
    def test_ranks_title_hits_first_and_cites_paths(self, vault):
        res = run(action="search", query="strategy reels")
        assert res.success
        assert res.data["results"][0]["rel"].endswith("Delta_Social_Strategy.md")
        assert res.message.splitlines()[0].startswith("Found")

    def test_no_hits_is_success_not_failure(self, vault):
        res = run(action="search", query="zzzxqj")
        assert res.success and res.data["results"] == []

    def test_query_text_never_makes_the_status_line_read_as_failure(self, vault):
        from core.tool_result import looks_like_failure

        res = run(action="search", query="error handling failed timeout")
        assert not looks_like_failure(res.message)

    def test_limit_is_capped(self, vault):
        for i in range(15):
            (vault / "06 - Resources" / f"Cap_{i}.md").write_text(
                f"# c\n\ncapword {i}\n", encoding="utf-8"
            )
        assert len(run(action="search", query="capword", limit=99).data["results"]) == 10

    def test_empty_query_refused(self, vault):
        assert run(action="search", query="  ").error_type == "MissingArgument"


# ── create / append ─────────────────────────────────────────────────────────
class TestCreateAppend:
    def test_create_writes_owned_frontmatter(self, vault):
        res = run(action="create", title="Showroom Hours", content="Open at nine.", brand="delta")
        assert res.success
        text = (vault / "02 - Delta Cuisine" / "Showroom_Hours.md").read_text(encoding="utf-8")
        assert "brand: delta_cuisine" in text and "type: reference" in text
        assert "created_by: era" in text and text.rstrip().endswith("Open at nine.")

    def test_create_never_overwrites(self, vault):
        run(action="create", title="Once", content="first")
        res = run(action="create", title="Once", content="second")
        assert res.error_type == "NoteExists"
        assert "second" not in (vault / "04 - Global" / "Once.md").read_text(encoding="utf-8")

    def test_unknown_brand_is_refused_not_guessed(self, vault):
        res = run(action="create", title="X", content="y", brand="Delta Quartz")
        assert res.error_type == "UnknownBrand"

    def test_append_adds_provenance_and_keeps_existing(self, vault):
        res = run(action="append", title="House Rules", content="Say bonjour first.")
        assert res.success
        text = (vault / "04 - Global" / "House_Rules.md").read_text(encoding="utf-8")
        assert "Always cite the showroom address." in text
        assert "by era -->" in text and text.rstrip().endswith("Say bonjour first.")

    def test_empty_content_refused(self, vault):
        res = run(action="append", title="House Rules", content=" ")
        assert res.error_type == "MissingArgument"

    def test_crlf_note_stays_crlf(self, vault):
        p = vault / "04 - Global" / "Crlf.md"
        p.write_bytes(b"# t\r\n\r\nline one\r\n")
        assert run(action="append", path="04 - Global/Crlf.md", content="line two").success
        raw = p.read_bytes()
        assert b"line two" in raw and raw.count(b"\n") == raw.count(b"\r\n")

    def test_no_temp_files_left_behind(self, vault):
        run(action="append", title="House Rules", content="x")
        assert not list(vault.rglob("*.tmp"))


# ── concurrency ─────────────────────────────────────────────────────────────
class TestConcurrency:
    def test_threads_lose_no_line(self, vault):
        errors = []

        def worker(i):
            res = run(action="append", title="House Rules", content=f"thread-line-{i}")
            if not res.success:
                errors.append(res.message)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        text = (vault / "04 - Global" / "House_Rules.md").read_text(encoding="utf-8")
        assert not errors
        assert all(text.count(f"thread-line-{i}") == 1 for i in range(10))

    def test_processes_lose_no_line(self, vault, tmp_path):
        script = (
            "import sys; sys.path.insert(0, sys.argv[1]);"
            "from core import vault_bridge as vb;"
            "r = [vb.run({'action':'append','title':'House Rules',"
            "'content':f'proc-{sys.argv[2]}-{i}'}) for i in range(4)];"
            "sys.exit(0 if all(x.success for x in r) else 3)"
        )
        env = {
            **os.environ,
            "ERA_VAULT_DIR": str(vault),
            "ERA_VAULT_VERIFY_SETTLE_S": "0",
            "ERA_MEMORY_DIR": os.environ.get("ERA_MEMORY_DIR", str(tmp_path / "mem")),
            "ERA_LOCK_FILE": str(tmp_path / "lk" / "era.lock"),
        }
        procs = [
            subprocess.Popen([sys.executable, "-c", script, str(REPO), str(n)], env=env)
            for n in range(3)
        ]
        codes = [p.wait(timeout=120) for p in procs]
        assert codes == [0, 0, 0]
        text = (vault / "04 - Global" / "House_Rules.md").read_text(encoding="utf-8")
        assert all(text.count(f"proc-{n}-{i}") == 1 for n in range(3) for i in range(4))


# ── the write contract ──────────────────────────────────────────────────────
class TestWriteContract:
    def test_sync_clobber_is_reported_as_failure(self, vault, monkeypatch):
        real_replace = os.replace

        def clobbering_replace(src, dst):
            real_replace(src, dst)
            Path(dst).write_bytes(b"OneDrive says hi")

        monkeypatch.setattr(vault_bridge.os, "replace", clobbering_replace)
        res = run(action="append", title="House Rules", content="will not stick")
        assert not res.success and res.error_type == "WriteVerificationFailed"

    def test_late_clobber_after_settle_is_caught(self, vault, monkeypatch):
        state = {"reads": 0}
        real_read = Path.read_bytes

        def flaky_read(self):
            data = real_read(self)
            if self.name == "House_Rules.md" and state.get("armed"):
                state["reads"] += 1
                if state["reads"] >= 2:  # first verify read is clean, second is clobbered
                    return b"rewritten later"
            return data

        real_replace = os.replace

        def arm_after_replace(src, dst):
            real_replace(src, dst)
            state["armed"] = True

        monkeypatch.setattr(Path, "read_bytes", flaky_read)
        monkeypatch.setattr(vault_bridge.os, "replace", arm_after_replace)
        monkeypatch.setenv("ERA_VAULT_VERIFY_SETTLE_S", "0.01")
        res = run(action="append", title="House Rules", content="late")
        assert not res.success and res.error_type == "WriteVerificationFailed"

    def test_lock_held_elsewhere_fails_loudly_and_writes_nothing(self, vault, monkeypatch):
        from contextlib import contextmanager

        @contextmanager
        def busy(*a, **k):
            raise vault_bridge.VaultLockUnavailable("another writer holds the vault lock")
            yield  # pragma: no cover

        monkeypatch.setattr(vault_bridge, "vault_write_lock", busy)
        path = vault / "04 - Global" / "House_Rules.md"
        before = path.read_text(encoding="utf-8")
        res = run(action="append", title="House Rules", content="blocked")
        assert not res.success and res.error_type == "LockUnavailable"
        assert path.read_text(encoding="utf-8") == before

    def test_stale_lock_file_from_a_crash_is_broken(self, vault):
        lock = vault_bridge._lock_path()
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("99999")
        old = os.path.getmtime(lock) - 600
        os.utime(lock, (old, old))
        assert run(action="append", title="House Rules", content="after a crash").success
        assert not lock.exists()

    def test_live_lock_blocks_then_times_out_and_is_left_alone(self, vault):
        lock = vault_bridge._lock_path()
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("12345")
        try:
            with pytest.raises(vault_bridge.VaultLockUnavailable):
                with vault_bridge.vault_write_lock(timeout=0.2):
                    pytest.fail("must not enter while another process holds the lock")
            assert lock.exists()  # a live holder's lock is never broken
        finally:
            lock.unlink()

    def test_lock_is_released_when_the_body_raises(self, vault):
        with pytest.raises(RuntimeError):
            with vault_bridge.vault_write_lock():
                raise RuntimeError("boom")
        assert not vault_bridge._lock_path().exists()
        with vault_bridge.vault_write_lock(timeout=0.5):
            pass

    def test_missing_vault_fails_every_action_without_raising(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cm_vault, "VAULT_DIR", tmp_path / "nowhere")
        for action in vault_bridge.ACTIONS:
            res = vault_bridge.run({"action": action, "title": "x", "content": "y", "query": "z"})
            assert not res.success and res.error_type == "VaultUnavailable"


# ── staged replace ──────────────────────────────────────────────────────────
class TestStagedReplace:
    P = "02 - Delta Cuisine/Delta_Social_Strategy.md"

    def test_section_replace_applies_only_after_staging(self, vault):
        res = stage_and_apply(self.P, "Seven reels a week.", section="Cadence")
        assert res.success
        text = (vault / self.P).read_text(encoding="utf-8")
        assert "Seven reels a week." in text and "Five reels" not in text
        assert "Warm." in text and "brand: delta_cuisine" in text

    def test_whole_body_replace_preserves_frontmatter(self, vault):
        assert stage_and_apply(self.P, "# New\n\nEverything new.").success
        text = (vault / self.P).read_text(encoding="utf-8")
        assert text.startswith("---\nbrand: delta_cuisine") and "Everything new." in text
        assert "Warm." not in text

    def test_stage_returns_a_diff_and_writes_nothing(self, vault):
        before = (vault / self.P).read_text(encoding="utf-8")
        pid, summary, diff = vault_bridge.stage_replace(
            Caller(), {"path": self.P, "content": "Seven reels.", "section": "Cadence"}
        )
        assert "-Five reels a week." in diff and "+Seven reels." in diff
        assert "Replace in" in summary
        assert (vault / self.P).read_text(encoding="utf-8") == before

    def test_plan_is_single_use(self, vault):
        pid, _, _ = vault_bridge.stage_replace(
            Caller(), {"path": self.P, "content": "Once.", "section": "Cadence"}
        )
        first = vault_bridge.run({"action": "replace", "_approved_plan": {"plan_id": pid}})
        second = vault_bridge.run({"action": "replace", "_approved_plan": {"plan_id": pid}})
        assert first.success and second.error_type == "NotStaged"

    @pytest.mark.parametrize("forged", [None, {}, {"plan_id": "deadbeef"}, "plan", {"plan_id": 1}])
    def test_forged_or_missing_plan_refused(self, vault, forged):
        res = vault_bridge.run({"action": "replace", "_approved_plan": forged})
        assert res.error_type == "NotStaged"

    def test_note_changed_since_staging_is_a_conflict_and_writes_nothing(self, vault):
        pid, _, _ = vault_bridge.stage_replace(
            Caller(), {"path": self.P, "content": "Stale plan.", "section": "Cadence"}
        )
        run(action="append", path=self.P, content="a human edited this meanwhile")
        snapshot = (vault / self.P).read_text(encoding="utf-8")
        res = vault_bridge.run({"action": "replace", "_approved_plan": {"plan_id": pid}})
        assert res.error_type == "Conflict"
        assert (vault / self.P).read_text(encoding="utf-8") == snapshot

    def test_missing_section_does_not_fall_back_to_append(self, vault):
        with pytest.raises(vault_bridge._Refusal) as exc:
            vault_bridge.stage_replace(
                Caller(), {"path": self.P, "content": "x", "section": "Nonexistent"}
            )
        assert exc.value.error_type == "SectionNotFound"

    def test_noop_replace_refused(self, vault):
        with pytest.raises(vault_bridge._Refusal) as exc:
            vault_bridge.stage_replace(
                Caller(), {"path": self.P, "content": "Five reels a week.", "section": "Cadence"}
            )
        assert exc.value.error_type == "NoChange"

    def test_preview_builder_carries_only_a_plan_id(self, vault):
        text, carried = vault_bridge.plan_replace_preview(
            {"action": "replace", "path": self.P, "content": "Eight.", "section": "Cadence"}
        )
        assert "Replace in" in text
        assert set(carried) == {"_approved_plan"}
        assert set(carried["_approved_plan"]) == {"plan_id"}
        assert vault_bridge.run({"action": "replace", **carried}).success

    def test_preview_builder_refusal_carries_nothing(self, vault):
        text, carried = vault_bridge.plan_replace_preview(
            {"action": "replace", "path": "04 - Global/Missing.md", "content": "x"}
        )
        assert carried == {} and "can't stage" in text.lower()


# ── isolation and zones ─────────────────────────────────────────────────────
class TestScopedCallers:
    def test_scoped_bot_cannot_read_another_brands_note(self, vault):
        res = run(action="read", path="03 - Ceram Quartz/Ceram_Pricing.md", caller=DELTA_BOT)
        assert not res.success and res.error_type == "NoteNotFound"  # same answer as "missing"

    def test_scoped_bot_search_hides_other_brand_but_shows_own_and_global(self, vault):
        res = run(action="search", query="reels rules slabs secret", caller=DELTA_BOT)
        rels = {r["rel"] for r in res.data["results"]}
        assert any("Delta_Social_Strategy" in r for r in rels)
        assert any("House_Rules" in r for r in rels)
        assert not any("Ceram_Pricing" in r for r in rels)

    def test_unreadable_brand_catalog_fails_closed(self, vault, monkeypatch):
        monkeypatch.setattr(vault_bridge, "_brand_catalog", lambda: None)
        assert run(action="read", title="Delta Social Strategy", caller=DELTA_BOT).success
        res = run(action="read", title="House Rules", caller=DELTA_BOT)
        assert not res.success  # cannot prove it is global, so not shown

    def test_scoped_bot_cannot_write_outside_its_zones(self, vault):
        res = run(
            action="append",
            path="02 - Delta Cuisine/Delta_Social_Strategy.md",
            content="x",
            caller=DELTA_BOT,
        )
        assert not res.success and res.error_type == "ZoneRefused"
        text = (vault / "02 - Delta Cuisine" / "Delta_Social_Strategy.md").read_text("utf-8")
        assert "by maya" not in text

    def test_scoped_bot_create_lands_in_the_unbriefed_inbox(self, vault):
        res = run(
            action="create", title="Maya Draft", content="Draft.", brand="delta", caller=DELTA_BOT
        )
        assert res.success and res.data["rel"] == "00 - Inbox/Maya_Draft.md"
        note = cm_vault._load_note(vault / "00 - Inbox" / "Maya_Draft.md")
        assert note.frontmatter["brand"] == "delta_cuisine" and not note.briefable

    def test_scoped_bot_cannot_create_for_another_brand(self, vault):
        res = run(action="create", title="Sneaky", content="x", brand="ceram", caller=DELTA_BOT)
        assert res.error_type == "BrandOutOfScope"

    def test_inbox_write_allowed(self, vault):
        res = run(action="append", path="00 - Inbox/Scratch.md", content="ok", caller=DELTA_BOT)
        assert res.success

    def test_replace_is_user_reviewed_so_it_is_not_zone_limited(self, vault):
        """Zones guard FREE writes. A replace needs the user's yes, so a scoped bot may
        stage one on a brand note it can read — and only on one it can read."""
        own = "02 - Delta Cuisine/Delta_Social_Strategy.md"
        pid, _, _ = vault_bridge.stage_replace(
            DELTA_BOT, {"path": own, "content": "Six reels.", "section": "Cadence"}
        )
        assert vault_bridge.run({"action": "replace", "_approved_plan": {"plan_id": pid}}).success
        with pytest.raises(vault_bridge._Refusal) as exc:
            vault_bridge.stage_replace(
                DELTA_BOT, {"path": "03 - Ceram Quartz/Ceram_Pricing.md", "content": "x"}
            )
        assert exc.value.error_type == "NoteNotFound"  # isolation still holds
