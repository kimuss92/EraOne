"""Guards for the 2026-11-17 sweep, round 2 (PF-042)."""

import pytest

# ── 1. A plain shutdown is never resumed into the next boot ─────────────────────


class _UI:
    muted = False

    def write_log(self, *_):
        pass


class _Manager:
    def __init__(self):
        self.current_resumption_token = "tok"
        self.saved = []

    def clear_token(self):
        self.current_resumption_token = None

    def save_to_disk(self):
        self.saved.append(self.current_resumption_token)
        return True


def _era(tool_dispatch):
    class _Era(tool_dispatch.ToolDispatchMixin):
        def __init__(self):
            self.ui = _UI()
            self.spoken = []
            self._shutdown_requested = False
            self._shutdown_awaiting_confirm = False
            self._is_speaking = False
            self.session_manager = _Manager()

        def speak(self, text):
            self.spoken.append(text)

    return _Era()


@pytest.fixture
def dispatch(tmp_path, monkeypatch):
    from app.era_live import tool_dispatch

    monkeypatch.setenv("ERA_HOME", str(tmp_path))
    monkeypatch.setenv("ERA_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("ERA_SUPERVISED", "1")

    class _NoThread:  # the real thread would exit the test process
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

    monkeypatch.setattr(tool_dispatch.threading, "Thread", _NoThread)
    import era_dispatch

    monkeypatch.setattr(era_dispatch, "current_job", lambda: None)
    return tool_dispatch


def test_plain_shutdown_discards_the_saved_session(dispatch):
    era = _era(dispatch)
    era._shutdown_era({})
    assert era.session_manager.current_resumption_token is None
    assert era.session_manager.saved == [None]
    assert era._discard_session_on_exit is True


def test_reboot_keeps_the_session(dispatch):
    era = _era(dispatch)
    assert era._shutdown_era({"relaunch": True}) == "Relaunching..."
    assert era.session_manager.current_resumption_token == "tok"
    assert not getattr(era, "_discard_session_on_exit", False)


# ── 2. "close Notepad without saving" names Notepad ─────────────────────────────


@pytest.mark.parametrize(
    "said",
    [
        "Notepad without saving",
        "Notepad and don't save",
        "Notepad, do not save it",
        "Notepad and discard changes",
    ],
)
def test_close_mode_words_are_not_part_of_the_title(said):
    from actions.window_control import _strip_close_mode

    assert _strip_close_mode(said) == "Notepad"


def test_a_real_title_is_untouched():
    from actions.window_control import _strip_close_mode

    assert _strip_close_mode("Saving Private Ryan - VLC") == "Saving Private Ryan - VLC"


# ── 3. A Spotify query searches, it never resumes the paused track ──────────────


def test_router_sends_a_spotify_query_to_search():
    from core.self_healing_router import route_tool_call

    tool, params = route_tool_call(
        "media_coordinator", {"action": "play", "target": "spotify"}, "Play lofi hip hop on Spotify"
    )
    assert tool == "spotify_control" and params["action"] == "search"
    assert "lofi hip hop" in params["value"]


def test_plain_resume_is_still_a_resume():
    from core.self_healing_router import route_tool_call

    tool, params = route_tool_call(
        "media_coordinator", {"action": "play", "target": "spotify"}, "resume Spotify"
    )
    assert (tool, params["action"]) == ("media_coordinator", "play")


def test_media_coordinator_spotify_query_searches(monkeypatch):
    pytest.importorskip("win32api")
    from actions import media_coordinator as mc
    from actions import spotify_control as sc

    calls = []
    monkeypatch.setattr(sc, "spotify_control", lambda p, player=None: calls.append(p) or "ok")
    monkeypatch.setattr(mc, "_pause_spotify_safely", lambda player=None: None)
    from core import media_state

    monkeypatch.setattr(media_state, "enforce_single_playback", lambda *a, **k: None)
    monkeypatch.setattr(mc.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        mc, "computer_settings", lambda *a, **k: pytest.fail("resumed instead of searching")
    )
    mc.media_coordinator({"action": "play", "target": "spotify", "query": "lofi hip hop"})
    assert calls == [{"action": "search", "value": "lofi hip hop"}]


@pytest.mark.parametrize("query", ["lofi hip hop", "jazz", "relaxing piano", "arabic rai"])
def test_a_bare_genre_is_a_mood_search(query):
    from core.personal_media import is_mood_request

    assert is_mood_request(query)


@pytest.mark.parametrize(
    "query",
    ["Hotel California", "Rap God", "Smooth Criminal", "Deep Purple", "jazz by Norah Jones"],
)
def test_a_title_with_a_genre_word_is_still_a_title(query):
    from core.personal_media import is_mood_request

    assert not is_mood_request(query)


def test_typed_yes_hands_the_armed_call_over_without_consuming_it():
    """PF-011 was green while broken: main.py consumed the confirmation, then the
    dispatcher's own confirm() found nothing, so a typed "yes" never ran the action.
    The typed-yes block must leave the single consume to the dispatcher."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    start = src.index("Typed confirmation reply detected")
    block = src[start : src.index("Check for negative/cancel reply", start)]
    assert "_pending_store.confirm(" not in block
    assert '"confirmed": True' in block and "_run_typed_approval" in block


def test_clock_reads_the_real_local_time():
    from datetime import datetime

    from actions.computer_control import computer_control

    out = computer_control({"action": "clock"})
    assert out.startswith("Local time: ") and datetime.now().strftime("%H:") in out
    assert "UTC" in out  # the offset is named so the model never guesses


def test_clock_is_declared_read_only_and_not_rerouted():
    from app import tool_declarations as td
    from core.action_registry import canonicalize_tool_call
    from core.completion_contract import is_read_only

    assert "clock" in str(td.__dict__)
    assert is_read_only("computer_control", "clock")
    assert canonicalize_tool_call("computer_control", {"action": "clock"})[0] == "computer_control"


@pytest.mark.parametrize(
    "said",
    ["What time is it?", "what's the time", "Era, what time is it now?", "quelle heure est-il"],
)
def test_a_typed_time_question_is_recognised(said):
    import main

    assert main._TIME_QUESTION.match(said.strip())


@pytest.mark.parametrize(
    "said", ["what time is it in Tokyo?", "remind me what time the meeting is", "time to go"]
)
def test_other_time_sentences_go_to_the_model(said):
    import main

    assert not main._TIME_QUESTION.match(said.strip())


def test_typed_approval_reports_its_outcome():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    body = src[src.index("async def _run_typed_approval") :][:1200]
    assert "spoken_outcome" in body and "self.speak(" in body


@pytest.mark.parametrize(
    "a,b",
    [
        (
            ("spotify_control", {"action": "search", "value": "some jazz"}),
            ("spotify_control", {"action": "search", "value": "jazz"}),
        ),
        (
            ("youtube_video", {"action": "play", "query": "lofi girl"}),
            ("youtube_video", {"action": "play", "query": "Lofi  Girl"}),
        ),
    ],
)
def test_the_same_media_start_has_the_same_key(a, b):
    from app.era_live.tool_dispatch import _media_start_key

    assert _media_start_key(*a) == _media_start_key(*b) is not None


def test_pause_and_status_are_never_deduped():
    from app.era_live.tool_dispatch import _media_start_key

    assert _media_start_key("spotify_control", {"action": "pause"}) is None
    assert _media_start_key("youtube_video", {"action": "youtube_pause"}) is None
    assert _media_start_key("netflix_control", {"action": "search", "value": "x"}) is None


def test_read_with_a_parent_folder_reads_inside_it(tmp_path, monkeypatch):
    from actions import file_controller as fc

    (tmp_path / "TODO.md").write_text("# board", encoding="utf-8")
    monkeypatch.setattr(fc, "_known_folder", lambda parent: tmp_path)
    monkeypatch.setattr(fc, "is_safe_path", lambda *a, **k: True)
    out = fc.file_controller({"action": "read", "name": "TODO.md", "parent": "EraOne"})
    assert "# board" in out


@pytest.mark.parametrize("order", ["status", "CM status", "what is running", "job status"])
def test_a_cm_status_question_never_starts_a_job(order):
    from app.era_live.tool_dispatch import _CM_STATUS_QUESTION

    assert _CM_STATUS_QUESTION.match(order)


@pytest.mark.parametrize(
    "order", ["publish the Perla reel", "make a status post for Delta", "quick publish status reel"]
)
def test_a_real_cm_order_is_still_an_order(order):
    from app.era_live.tool_dispatch import _CM_STATUS_QUESTION

    assert not _CM_STATUS_QUESTION.match(order)


@pytest.mark.parametrize("said", ["wake up", "Era, wake up", "come back online", "wake up please"])
def test_a_typed_wake_order_wakes_from_standby(said):
    import main

    assert main._TYPED_WAKE_ORDER.match(said.lower().strip())


@pytest.mark.parametrize(
    "said", ["what's the weather", "don't wake up the kids", "wake me up at 7"]
)
def test_other_typed_text_does_not_wake(said):
    import main

    assert not main._TYPED_WAKE_ORDER.match(said.lower().strip())


def test_a_bridge_question_learns_the_bridge_is_off(monkeypatch):
    import types as _t

    from app import tool_declarations
    from app.era_live import tool_dispatch as td
    from core.tool_result import ToolResult

    monkeypatch.setattr(tool_declarations, "_withheld", {"spotify_bridge"})
    era = _t.SimpleNamespace(_inflight_user_text="Is the Spotify bridge connected?")
    out = td._note_bridge_is_off(era, "spotify_control", ToolResult.ok("Spotify paused: 'x'."))
    assert "SWITCHED OFF" in out.message and out.success


def test_an_ordinary_spotify_question_is_untouched(monkeypatch):
    import types as _t

    from app import tool_declarations
    from app.era_live import tool_dispatch as td
    from core.tool_result import ToolResult

    monkeypatch.setattr(tool_declarations, "_withheld", {"spotify_bridge"})
    era = _t.SimpleNamespace(_inflight_user_text="What song is this?")
    res = ToolResult.ok("Spotify playing: 'x'.")
    assert td._note_bridge_is_off(era, "spotify_control", res) is res


@pytest.mark.parametrize(
    "said",
    [
        "Summarize the file handoff.md in my EraOne folder",
        "summarize my unread emails",
        "analyze what's on my screen",
        "compare report.pdf and plan.docx",
    ],
)
def test_work_on_local_data_stays_on_the_tool_model(said):
    from core.llm.taskclass import NAVIGATION_STATUS, classify

    assert classify(said).task == NAVIGATION_STATUS


@pytest.mark.parametrize(
    "said", ["Summarize the history of the Roman empire", "research the best laptops of 2026"]
)
def test_general_research_still_routes_to_reasoning(said):
    from core.llm.taskclass import RESEARCH_SYNTHESIS, classify

    assert classify(said).task == RESEARCH_SYNTHESIS


def test_a_url_is_never_read_aloud():
    import types as _t

    from app.era_live.speak_once import spoken_outcome

    res = _t.SimpleNamespace(
        success=True,
        message=(
            "Search failed, sir: error sending request for url (https://search.yahoo.com/"
            "search;_ylt=abc?p=Find+the+title) > operation timed out (Answered via web_search"
            " — agent-browser's `chat` mode needs a Vercel AI Gateway key (AI_GATEWAY_A"
        ),
        data={},
    )
    said = spoken_outcome("agent_browser", res)
    assert "http" not in said and "yahoo" not in said.lower() and "AI_GATEWAY" not in said
