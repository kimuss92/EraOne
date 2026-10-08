from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_approval_prompt_is_in_the_left_activity_rail():
    app = (ROOT / "ui_src/src/App.tsx").read_text(encoding="utf-8")
    left_start = app.index('<aside className={`rail rail-left')
    left_end = app.index("</aside>", left_start)
    prompt = app.index("<ApprovalPrompt />")

    assert left_start < prompt < left_end


def test_approval_buttons_use_the_same_yes_no_command_path():
    prompt = (ROOT / "ui_src/src/components/ApprovalPrompt.tsx").read_text(encoding="utf-8")

    assert "Approve" in prompt
    assert "Cancel" in prompt
    assert "actions.sendCommand(v)" in prompt
    assert "answer('yes')" in prompt
    assert "answer('no')" in prompt
    assert 'say “yes” / “no”' in prompt
