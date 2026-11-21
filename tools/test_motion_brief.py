"""The Motion Design brief (Option A: crew -> Claude Code session): fixed format, logos and
textures resolved by code, the owner's ask-first French-or-no-text rule, vertical format."""

import re
from types import SimpleNamespace

import pytest

from crew import capabilities as caps
from crew import motion_brief as mb

SHOTS = [
    "0-4 s: ALYA hero panel on a dark teal set, slow turn, light sweeps across the slab",
    "4-8 s: the panel transforms into PALETINO, the vein line continues across",
    "8-12 s: HAZE BREZEE and TAJ ROYAL slide through in a row",
    "12-16 s: logo end card",
]


def _brief(**kw):
    args = dict(length_s=16, textures=["Alya", "PALETINO", "HAZE BREZEE", "TAJ ROYAL"],
                reference_url="https://pin.it/7kpPgdqcK", reference_notes="hero turn, match-cut, name card, logo card")
    args.update(kw)
    return mb.render_brief("ceram_quartz", "Alya test reel", SHOTS, "Logo on the last shot", **args)


def test_the_brief_carries_logos_textures_format_and_the_ask_first_text_rule():
    text = _brief()
    assert mb.validate_brief(text) == []
    for needle in ("text_mode: ask", "storyboard_approved: pending", "format: 1080x1920",
                   "FRENCH or NONE", "ONE question", "logo ceram transparent.png",
                   "logo ceram.png", "Alya", "inspiration only"):
        assert needle in text, needle
    assert text.index("## Text decision (ASK FIRST)") < text.index("## Storyboard")
    assert "1. 0-4 s" in text and "4. 12-16 s" in text


def test_textures_are_resolved_and_a_low_resolution_one_is_reported():
    tex = mb.resolve_textures(["Alya", "alya.jpg", "does not exist"], mb.motion_config("ceram_quartz")["textures_dir"])
    assert tex[0]["exists"] and "alya." in tex[0]["path"].lower()  # the owner replaced the files (now Alya.png)
    assert re.fullmatch(r"\d+x\d+", tex[0]["size"])  # the real pixel size is reported, whatever the file is
    assert tex[1]["exists"] and not tex[2]["exists"]
    assert "NOT FOUND" in _brief(textures=["does not exist"])


def test_a_brief_with_arabic_or_no_storyboard_or_no_logo_config_is_flagged():
    arabic = mb.render_brief("ceram_quartz", "x", ["1 مرحبا"], "end")
    assert any("Arabic" in p for p in mb.validate_brief(arabic))
    assert any("no numbered shots" in p for p in mb.validate_brief(mb.render_brief("ceram_quartz", "x", [], "end")))
    no_logo = mb.render_brief("ceram_quartz", "x", SHOTS, "end", motion={"format": "1080x1920"})
    assert any("logos" in p for p in mb.validate_brief(no_logo))


def test_both_brands_have_existing_logo_files_and_the_ask_policy():
    for brand in ("delta_cuisine", "ceram_quartz"):
        m = mb.motion_config(brand)
        assert m["text_policy"] == "ask" and m["format"] == "1080x1920" and m["text_language"] == "French"
        from pathlib import Path

        for name in ("transparent", "with_background"):
            assert Path(m["logos"][name]).is_file(), (brand, name)


class _Vault:
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    def run(self, params, caller=None):
        self.calls.append(params)
        from core.tool_result import ToolResult

        if not self.ok:
            return ToolResult.fail("no", error_type="Refused")
        return ToolResult.ok(message="Created", data={"rel": "00 - Inbox/Motion_brief_Alya_test_reel.md"})


def _ctx():
    return SimpleNamespace(caller=object())


def test_tom_saves_a_brief_through_the_vault_and_gets_the_sentence_to_say(monkeypatch):
    vault = _Vault()
    monkeypatch.setattr(caps, "vault_bridge", vault)
    cap = caps.CAPABILITIES["motion.brief"]
    res = cap.fn(_ctx(), {"brand": "Ceram Quartz", "title": "Alya test reel", "shots": SHOTS,
                          "ending": "logo", "textures": ["Alya"]})
    assert res.success and "Nothing is built yet" in res.message
    assert 'start motion design from this brief: "Motion brief Alya test reel"' in res.message
    sent = vault.calls[0]
    assert sent["title"] == "Motion brief Alya test reel"
    assert 'from this brief: "Motion brief Alya test reel"' in sent["content"]  # body and spoken sentence agree
    assert sent["action"] == "create" and sent["brand"] == "ceram_quartz" and sent["note_type"] == "plan"
    assert "text_mode: ask" in sent["content"] and "ONE question" in sent["content"]


def test_the_capability_refuses_without_a_storyboard_or_for_an_unknown_brand(monkeypatch):
    monkeypatch.setattr(caps, "vault_bridge", _Vault())
    cap = caps.CAPABILITIES["motion.brief"]
    assert not cap.fn(_ctx(), {"brand": "ceram_quartz", "title": "t", "shots": []}).success
    assert not cap.fn(_ctx(), {"brand": "nobody", "title": "t", "shots": SHOTS}).success
    assert not _Vault(False).run({}).success


def test_a_shots_text_block_with_one_line_per_shot_is_accepted():
    assert caps._as_list("1. a\n2. b\n\n- c") == ["1. a", "2. b", "c"]
    assert caps._as_list(["x", " ", "y"]) == ["x", "y"]


def test_tom_holds_the_capability_and_the_crew_registry_still_loads():
    from crew import registry

    report = registry.load_specs()
    tom = next(s for s in report.specs.values() if s.id == "tom") if hasattr(report, "specs") else None
    assert tom is not None and "motion.brief" in tom.capabilities


def test_structured_shots_are_written_as_readable_lines_not_dict_reprs():
    shots = caps._as_list([{"time": "0.0s", "frame": "Hero", "one_line": "ALYA turns", "what_moves": "push in",
                            "how_it_becomes_next": "match-cut on the vein"}, "2. plain text"])
    assert shots[0] == "0.0s | Hero | ALYA turns | push in | match-cut on the vein"
    assert "{" not in shots[0] and shots[1] == "2. plain text"


def test_the_brief_has_one_title_heading_because_the_vault_adds_its_own():
    assert "# Motion brief" not in _brief()


def test_style_warnings_catch_the_rules_tom_broke_live():
    bad = ["Camera zooms in on ALYA", "white flash then cross dissolve to PALETINO", "later it zooms out; kaleidoscope"]
    w = mb.style_warnings(bad)
    assert any("zooms in AND out" in x for x in w) and any("flash" in x for x in w)
    assert any("dissolve" in x for x in w) and any("kaleidoscope" in x for x in w)
    good = ["0-4 s: ALYA turns slowly, the vein becomes the edge of PALETINO", "4-8 s: match-cut onto PALETINO, the push continues"]
    assert mb.style_warnings(good) == []
    assert any("becomes the next" in x for x in mb.style_warnings(["a still frame", "another still frame"]))


def test_the_capability_reports_style_problems_to_the_caller(monkeypatch):
    vault = _Vault()
    monkeypatch.setattr(caps, "vault_bridge", vault)
    res = caps.CAPABILITIES["motion.brief"].fn(_ctx(), {"brand": "ceram_quartz", "title": "t",
        "shots": ["zooms in", "zooms out", "cross dissolve"], "ending": "logo"})
    assert res.success and "style:" in res.message and "Problems to fix before building" in res.message


def test_the_house_rules_are_in_the_skill_in_all_four_installed_copies():
    """Ask-first French-or-no-text, start from a brief, vertical, reference, huge-logo copy."""
    import hashlib
    from pathlib import Path

    home = Path.home()
    repo = Path(__file__).resolve().parent.parent / ".claude" / "skills" / "motion-design" / "SKILL.md"
    copies = [repo] + [home / d / "skills" / "motion-design" / "SKILL.md" for d in (".claude", ".claude-delta", ".claude-freelance")]
    texts = {c: c.read_text(encoding="utf-8") for c in copies if c.is_file()}
    assert repo in texts
    for needle in ("Era One house rules", "ASK FIRST, before ANY work", "en français, ou sans texte",
                   "start motion design from this brief", "1080x1920", "motion_reference.py",
                   "Never Arabic", "Huge logo files", "2000 px"):
        assert needle in texts[repo], needle
    assert len({hashlib.sha256(t.encode()).hexdigest() for t in texts.values()}) == 1, "installed copies differ"


def test_launch_info_picks_the_newest_brief_of_the_brand_with_the_allowed_folders(monkeypatch, tmp_path):
    from core import cm_vault

    inbox = tmp_path / "vault" / "00 - Inbox"
    inbox.mkdir(parents=True)
    old = inbox / "Motion_brief_Old_reel.md"
    new = inbox / "Motion_brief_Alya_test_reel_v2.md"
    other = inbox / "Motion_brief_Delta_promo.md"
    old.write_text("---\nbrand: ceram_quartz\n---\nbody", encoding="utf-8")
    new.write_text("---\nbrand: ceram_quartz\n---\nbody", encoding="utf-8")
    other.write_text("---\nbrand: delta_cuisine\n---\nbody", encoding="utf-8")
    import os

    os.utime(old, (1, 1))
    monkeypatch.setattr(cm_vault, "VAULT_DIR", tmp_path / "vault")
    info = mb.launch_info("ceram_quartz")
    assert info["ok"] and info["title"] == "Motion brief Alya test reel v2"
    assert info["sentence"] == 'start motion design from this brief: "Motion brief Alya test reel v2" (vault Inbox)'
    assert info["project_dir"].endswith("ceram quartz")
    assert str(tmp_path / "vault") in info["add_dirs"] and any("textures" in d.lower() for d in info["add_dirs"])
    assert len(info["add_dirs"]) == len(set(info["add_dirs"]))
    assert mb.launch_info("delta_cuisine")["title"] == "Motion brief Delta promo"
    assert mb.launch_info("ceram_quartz", "old")["title"] == "Motion brief Old reel"
    miss = mb.launch_info("ceram_quartz", "nothing like this")
    assert not miss["ok"] and "ask Tom" in miss["error"]


def test_the_launcher_escapes_the_quotes_windows_powershell_would_mangle():
    from pathlib import Path

    scripts = Path(__file__).resolve().parent.parent / "scripts"
    ps1 = (scripts / "start_motion_design.ps1").read_text(encoding="utf-8")
    assert "PSVersionTable.PSVersion.Major -lt 7" in ps1 and "-replace '\"', '\\\"'" in ps1
    assert "--add-dir" in ps1 and "DryRun" in ps1 and "launch-info" in ps1
    for forbidden in ("dangerously-skip-permissions", "--permission-mode", "bypassPermissions"):
        assert forbidden not in ps1, forbidden  # every prompt and approval still reaches Karim
    cmd = (scripts / "start_motion_design.cmd").read_text(encoding="utf-8")
    assert "start_motion_design.ps1" in cmd and "ExecutionPolicy Bypass" in cmd


def test_the_launcher_puts_the_prompt_before_the_variadic_add_dir_and_refreshes_the_gateway_env():
    """Seen live 2026-11-20: the trailing prompt was read as a value of --add-dir."""
    from pathlib import Path

    ps1 = (Path(__file__).resolve().parent.parent / "scripts" / "start_motion_design.ps1").read_text(encoding="utf-8")
    assert "& $claude $sentence @claudeArgs" in ps1 and "& $claude @claudeArgs $sentence" not in ps1
    assert 'GetEnvironmentVariable($name, "User")' in ps1 and "ANTHROPIC_API_KEY" in ps1
    assert "Write-Host $env:ANTHROPIC" not in ps1 and "echo $env:ANTHROPIC" not in ps1  # values are never printed


def test_the_launcher_passes_the_gateway_secret_as_a_bearer_token_for_this_window_only():
    """Seen live 2026-11-20: the key was on Claude Code's rejected list, so interactive Claude said
    "Login expired". A bearer token skips the approval check; nothing global is edited."""
    from pathlib import Path

    ps1 = (Path(__file__).resolve().parent.parent / "scripts" / "start_motion_design.ps1").read_text(encoding="utf-8")
    assert "$env:ANTHROPIC_AUTH_TOKEN = $env:ANTHROPIC_API_KEY" in ps1
    assert "Remove-Item Env:ANTHROPIC_API_KEY" in ps1
    for global_edit in ("SetEnvironmentVariable", "setx", ".claude.json", "customApiKeyResponses ="):
        assert global_edit not in ps1.replace("(~/.claude.json customApiKeyResponses)", ""), global_edit


def test_the_launcher_uses_the_claude_account_directly_in_its_own_profile_not_the_gateway():
    """Karim 2026-11-20: long interactive sessions broke on the OmniRoute fallback chain (tool_addition
    needs an anthropic-beta header, signed thinking blocks, other models' formats): use the Claude account."""
    from pathlib import Path

    ps1 = (Path(__file__).resolve().parent.parent / "scripts" / "start_motion_design.ps1").read_text(encoding="utf-8")
    default_branch = ps1.split("} else {", 1)[1].split("Write-Host \"\"", 1)[0]
    for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL"):
        assert f'"{name}"' in default_branch, name  # removed from the window in direct mode
    assert 'Join-Path $env:USERPROFILE ".claude-motion"' in ps1 and "$env:CLAUDE_CONFIG_DIR = $profileDir" in ps1
    assert "auth status" in ps1 and "/login" in ps1 and "[switch]$Gateway" in ps1
    assert "Copy-Item" in ps1 and "motion-design" in ps1  # the skill is kept in step with the repo
    assert '"Bash(node:*)","Bash(python:*)","Bash(ffmpeg:*)","Bash(ffprobe:*)"' in ps1
    assert "bypassPermissions" not in ps1 and "dangerously" not in ps1


def test_the_ceram_brief_carries_the_navy_3d_brand_style_and_never_the_old_teal_look():
    """Karim 2026-11-20: the first build looked like the OLD reference (flat panels, teal wall). The brand's
    motion style (navy studio, 3D slabs, template folder) is added to every Ceram brief by code."""
    text = _brief()
    assert "## Brand motion style" in text and "BACKGROUND" in text and "#103C4E" in text
    assert "never blue navy" in text.lower() and "White Quartz Premium Product Commercial" in text
    assert "_template_ceram_3d" in text and "AVOID" in text and "teal" in text.lower()
    assert not any("brand motion style section is missing" in p for p in mb.validate_brief(text))
    stripped = text.replace("## Brand motion style", "## x")
    assert any("brand motion style" in p for p in mb.validate_brief(stripped))


def test_the_skill_says_the_brand_style_overrides_the_old_teal_look():
    from pathlib import Path

    skill = (Path(__file__).resolve().parent.parent / ".claude" / "skills" / "motion-design" / "SKILL.md").read_text(encoding="utf-8")
    assert "never blue navy" in skill and "Slabs must look normal" in skill and "three.js" in skill
