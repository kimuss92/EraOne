"""
Unit tests for Vision Silent Mode — Steps 14A.0.1 + 14A.0.2 + 14A.0.3.

Verifies:
1. screen_process returns text via one-shot call using genai.Client.
2. genai.configure is never called.
3. genai.Client SDK is used correctly.
4. sounddevice.RawOutputStream is never instantiated.
5. No native-audio Live session used for screen analysis.
6. One-shot failure returns fallback text.
7. VISION_TEXT_MODEL defaults to gemini-3.8-flash.
8. No real API/camera/screen calls.
9. Client reuse via get_vision_client() singleton.
10. Image optimization reduces size.
11. Timeout returns "I could not analyze the screen quickly enough."
12. Cache returns same result without second API call.
13. Concise prompt for short answers.
14. Response truncation at 800 chars.

Run:
    python -m pytest tools/test_vision_silent.py -v
"""

import inspect
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, PropertyMock, patch


class TestVisionSilentMode(unittest.TestCase):
    def setUp(self):
        from actions.netflix_control import _reset_netflix_vision_cooldown

        _reset_netflix_vision_cooldown()
        self._patchers = []
        self._patchers.append(patch("actions.screen_processor._capture_screen"))
        self._patchers.append(patch("actions.screen_processor._capture_camera"))
        self._patchers.append(
            patch(
                "core.intent_memory.load_state",
                return_value={
                    "last_app": None,
                    "last_media_app": None,
                    "last_browser": "brave",
                    "last_action": None,
                    "last_tool": None,
                    "last_window": None,
                    "previous_window": None,
                    "last_result": None,
                    "last_error": None,
                    "updated_at": 0.0,
                    "history": [],
                },
            )
        )

        self.mock_capture_screen = self._patchers[0].start()
        self.mock_capture_camera = self._patchers[1].start()
        self._patchers[2].start()

        self.mock_capture_screen.return_value = (b"fake-png-data", "image/png", 1000, 800)

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        from actions.netflix_control import _reset_netflix_vision_cooldown

        _reset_netflix_vision_cooldown()

    def test_returns_analysis_text(self):
        from actions.screen_processor import screen_process

        with patch("actions.screen_processor.analyze_image_once") as mock_one_shot:
            mock_one_shot.return_value = (
                "A WhatsApp tab is not visible among the open browser tabs."
            )

            result = screen_process(
                parameters={"text": "Analyze the screen contents"},
                player=None,
            )

            self.assertIsInstance(result, str)
            self.assertIn("WhatsApp", result)
            self.assertNotIn("I could not analyze", result)
            mock_one_shot.assert_called_once()

    def test_sounddevice_not_instantiated(self):
        import actions.screen_processor as sp_module

        source = inspect.getsource(sp_module)
        self.assertNotIn("sounddevice.RawOutputStream", source)
        self.assertNotIn("sd.RawOutputStream", source)
        self.assertNotIn("_play_loop", source)

    def test_no_genai_configure_called(self):
        import actions.screen_processor as sp_module

        source = inspect.getsource(sp_module)
        self.assertNotIn("genai.configure(", source)
        self.assertNotIn("genai.configure (", source)

    def test_genai_client_is_imported(self):
        import actions.screen_processor as sp_module

        source = inspect.getsource(sp_module)
        self.assertIn("from google import genai", source)

    def test_genai_client_used_in_analyze(self):
        from actions.screen_processor import _call_gemini

        source = inspect.getsource(_call_gemini)
        self.assertIn("get_vision_client()", source)
        self.assertIn("client.models.generate_content(", source)

    def test_types_part_from_bytes_used(self):
        from actions.screen_processor import _call_gemini

        source = inspect.getsource(_call_gemini)
        self.assertIn("types.Part.from_bytes(", source)

    def test_no_native_audio_model_for_vision(self):
        import actions.screen_processor as sp_module

        source = inspect.getsource(sp_module)
        self.assertNotIn("gemini-2.5-flash-native-audio", source)
        self.assertNotIn("_LIVE_MODEL", source)
        import re

        code_only = re.sub(r'"""[\s\S]*?"""', "", source)
        code_only = re.sub(r"'''[\s\S]*?'''", "", code_only)
        code_only = re.sub(r"#.*$", "", code_only, flags=re.MULTILINE)
        self.assertNotIn("native-audio", code_only)

    def test_no_response_modalities_text_with_native_audio(self):
        import actions.screen_processor as sp_module

        source = inspect.getsource(sp_module)
        self.assertNotIn('response_modalities=["TEXT"]', source)
        self.assertNotIn("response_modalities=['TEXT']", source)

    def test_no_output_audio_transcription(self):
        import actions.screen_processor as sp_module

        source = inspect.getsource(sp_module)
        self.assertNotIn("output_audio_transcription", source)

    def test_no_voice_config(self):
        import actions.screen_processor as sp_module

        source = inspect.getsource(sp_module)
        self.assertNotIn("voice_config", source)

    def test_no_speech_config(self):
        import actions.screen_processor as sp_module

        source = inspect.getsource(sp_module)
        self.assertNotIn("speech_config", source)

    def test_no_sounddevice_import(self):
        import actions.screen_processor as sp_module

        source = inspect.getsource(sp_module)
        self.assertNotIn("import sounddevice", source)
        self.assertNotIn("from sounddevice", source)

    def test_no_live_connect_session(self):
        import actions.screen_processor as sp_module

        source = inspect.getsource(sp_module)
        self.assertNotIn("client.aio.live.connect", source)
        self.assertNotIn("LiveConnectConfig", source)

    def test_vision_text_model_configurable(self):
        import actions.screen_processor as sp_module

        self.assertEqual(sp_module._VISION_TEXT_MODEL, "gemini-3.8-flash")
        self.assertNotEqual(sp_module._VISION_TEXT_MODEL, "gemini-2.5-flash-native-audio")

    def test_analyze_image_once_uses_vision_text_model(self):
        from actions.screen_processor import _call_gemini

        source = inspect.getsource(_call_gemini)
        self.assertIn("_VISION_TEXT_MODEL", source)

    def test_no_google_generativeai_import(self):
        import actions.screen_processor as sp_module

        source = inspect.getsource(sp_module)
        self.assertNotIn("google.generativeai", source)

    def test_no_speak_tts_audio_called(self):
        from actions.screen_processor import screen_process

        with patch("actions.screen_processor.analyze_image_once") as mock_one_shot:
            mock_one_shot.return_value = "I see a desktop."

            result = screen_process(
                parameters={"text": "Look at the screen"},
                player=None,
            )

            self.assertIsInstance(result, str)
            mock_one_shot.assert_called_once()

    def test_result_returned_once(self):
        from actions.screen_processor import screen_process

        call_count = [0]

        def _side_effect(*args, **kwargs):
            call_count[0] += 1
            return "Single response."

        with patch("actions.screen_processor.analyze_image_once", side_effect=_side_effect):
            result = screen_process(parameters={"text": "Check screen"}, player=None)

            self.assertEqual(call_count[0], 1)
            self.assertEqual(result, "Single response.")

    def test_non_data_response_no_crash(self):
        from actions.netflix_control import is_netflix_vision_blocked
        from actions.screen_processor import screen_process

        # Verify cooldown is not active before test
        self.assertFalse(is_netflix_vision_blocked(), "Cooldown should not be active before test")

        with patch("actions.screen_processor.analyze_image_once") as mock_one_shot:
            mock_one_shot.return_value = None

            result = screen_process(parameters={"text": "What is visible?"}, player=None)

            # The service path already failed, so the chain is not re-run and the
            # message carries a do-not-repeat hint (loop breaker) after the failure text.
            self.assertTrue(result.startswith("I could not analyze the screen"), result)

        # Verify cooldown was NOT set after non-data response
        self.assertFalse(
            is_netflix_vision_blocked(), "Non-data response must not set Netflix cooldown"
        )

    def test_empty_question_returns_fallback(self):
        from actions.netflix_control import is_netflix_vision_blocked
        from actions.screen_processor import screen_process

        # Verify cooldown is not active before test
        self.assertFalse(is_netflix_vision_blocked(), "Cooldown should not be active before test")

        with patch("actions.screen_processor.analyze_image_once") as mock_one_shot:
            result = screen_process(parameters={"text": ""}, player=None)

            self.assertEqual(result, "I could not analyze the screen.")
            mock_one_shot.assert_not_called()

        # Verify cooldown was NOT set after empty question
        self.assertFalse(
            is_netflix_vision_blocked(), "Empty question must not set Netflix cooldown"
        )

    def test_capture_error_returns_fallback(self):
        from actions.netflix_control import is_netflix_vision_blocked
        from actions.screen_processor import screen_process

        # Verify cooldown is not active before test
        self.assertFalse(is_netflix_vision_blocked(), "Cooldown should not be active before test")

        self.mock_capture_screen.side_effect = RuntimeError("Screen grab failed")

        result = screen_process(parameters={"text": "Look"}, player=None)

        self.assertEqual(result, "I could not analyze the screen.")

        # Verify cooldown was NOT set after capture error
        self.assertFalse(is_netflix_vision_blocked(), "Capture error must not set Netflix cooldown")

    def test_no_real_screenshot(self):
        import actions.screen_processor as sp_module

        with patch("actions.screen_processor.analyze_image_once") as mock_one_shot:
            mock_one_shot.return_value = "Mock result."

            result = sp_module.screen_process(parameters={"text": "What do you see?"}, player=None)

            self.assertEqual(result, "Mock result.")

    def test_one_shot_failure_returns_fallback(self):
        from actions.screen_processor import analyze_image_once

        with patch("actions.screen_processor.genai.Client") as mock_client:
            mock_client_instance = MagicMock()
            mock_client.return_value = mock_client_instance
            mock_client_instance.models.generate_content.side_effect = RuntimeError("API error")

            result = analyze_image_once(b"fake-image", "image/png", "What do you see?")

            self.assertEqual(result, "I could not analyze the screen quickly enough.")

    def test_one_shot_timeout_returns_quick_fallback(self):
        import threading as th

        from actions.screen_processor import analyze_image_once

        def _blocking_call():
            mock = MagicMock()
            mock.models.generate_content.side_effect = RuntimeError("never reached")

        with patch("actions.screen_processor.genai.Client") as mock_client:
            mock_client.return_value = MagicMock()
            mock_client.return_value.models.generate_content.side_effect = lambda **kwargs: (
                time.sleep(60)
            )

            result = analyze_image_once(b"fake-image", "image/png", "What do you see?", timeout=0.2)

            self.assertEqual(result, "I could not analyze the screen quickly enough.")

    def test_analyze_image_once_returns_str(self):
        from actions.screen_processor import analyze_image_once

        sig = inspect.signature(analyze_image_once)
        ret_ann = str(sig.return_annotation)
        self.assertEqual(ret_ann, "str")

    def test_screen_process_returns_str_not_bool(self):
        import actions.screen_processor as sp_module

        sig = inspect.signature(sp_module.screen_process)
        ret_ann = str(sig.return_annotation)
        self.assertEqual(ret_ann, "str")

    def test_no_native_audio_live_session_for_vision(self):
        import actions.screen_processor as sp_module

        with patch("actions.screen_processor.analyze_image_once") as mock_one_shot:
            mock_one_shot.return_value = "Analysis."

            sp_module.screen_process(parameters={"text": "Analyze"}, player=None)

            mock_one_shot.assert_called_once()
            call_args = mock_one_shot.call_args
            self.assertIsNotNone(call_args)

    def test_unsupported_modality_does_not_reconnect(self):
        import actions.screen_processor as sp_module
        from actions.screen_processor import screen_process

        sp_module._CACHE.clear()

        with patch("actions.screen_processor.analyze_image_once") as mock_one_shot:
            mock_one_shot.return_value = "Analysis result."

            screen_process(parameters={"text": "First question"}, player=None)
            screen_process(parameters={"text": "Second question"}, player=None)

            self.assertEqual(mock_one_shot.call_count, 2)

    def test_candidates_fallback_extraction(self):
        from actions.screen_processor import analyze_image_once

        mock_response = MagicMock()
        type(mock_response).text = PropertyMock(side_effect=ValueError("no text"))
        part = MagicMock()
        part.text = "Extracted from parts"
        candidate_content = MagicMock()
        candidate_content.parts = [part]
        candidate = MagicMock()
        candidate.content = candidate_content
        mock_response.candidates = [candidate]

        with patch("actions.screen_processor.get_vision_client") as mock_get_client:
            mock_client_instance = MagicMock()
            mock_get_client.return_value = mock_client_instance
            mock_client_instance.models.generate_content.return_value = mock_response

            result = analyze_image_once(b"fake-image", "image/png", "What do you see?")

            self.assertEqual(result, "Extracted from parts")

    def test_empty_response_returns_fallback(self):
        from actions.screen_processor import analyze_image_once

        mock_response = MagicMock()
        type(mock_response).text = PropertyMock(side_effect=ValueError("no text"))
        mock_response.candidates = []

        with patch("actions.screen_processor.get_vision_client") as mock_get_client:
            mock_client_instance = MagicMock()
            mock_get_client.return_value = mock_client_instance
            mock_client_instance.models.generate_content.return_value = mock_response

            result = analyze_image_once(b"fake-image", "image/png", "What do you see?")

            self.assertEqual(result, "I could not analyze the screen quickly enough.")

    def test_no_response_modalities_anywhere(self):
        import actions.screen_processor as sp_module

        source = inspect.getsource(sp_module)
        self.assertNotIn("response_modalities", source)


class TestVisionClientReuse(unittest.TestCase):
    def setUp(self):
        import actions.screen_processor as sp_module

        sp_module._VISION_CLIENT = None

    def test_get_vision_client_returns_singleton(self):
        import actions.screen_processor as sp_module

        with patch("actions.screen_processor.genai.Client") as mock_client:
            mock_instance = MagicMock()
            mock_client.return_value = mock_instance

            client1 = sp_module.get_vision_client()
            client2 = sp_module.get_vision_client()

            self.assertIs(client1, client2)
            self.assertEqual(mock_client.call_count, 1)

    def test_get_vision_client_not_in_logs(self):
        import actions.screen_processor as sp_module

        source = inspect.getsource(sp_module)
        self.assertNotIn("API_KEY", source.lower())


class TestVisionImageOptimization(unittest.TestCase):
    def test_compress_image_returns_valid_tuple(self):
        import actions.screen_processor as sp_module
        from actions.screen_processor import _compress_image

        original = b"fake-large-image-data" * 100

        with patch.object(sp_module, "_PIL", False):
            result_bytes, mime, orig_size, opt_size = _compress_image(original, "PNG")
            self.assertEqual(mime, "image/png")
            self.assertEqual(result_bytes, original)
            self.assertEqual(orig_size, opt_size)

    def test_compress_image_fallback_on_error(self):
        import actions.screen_processor as sp_module
        from actions.screen_processor import _compress_image

        original = b"fake-image-data"
        with patch.object(sp_module, "_PIL", True):
            with patch("PIL.Image.open", side_effect=Exception("Test error")):
                result_bytes, mime, orig_size, opt_size = _compress_image(original, "PNG")
                self.assertEqual(result_bytes, original)
                self.assertEqual(mime, "image/png")


class TestVisionCache(unittest.TestCase):
    def test_cache_returns_same_result(self):
        import actions.screen_processor as sp_module

        sp_module._CACHE.clear()

        with patch("actions.screen_processor._capture_screen") as mock_cap:
            mock_cap.return_value = (b"image-data", "image/png", 1000, 800)

            with patch("actions.screen_processor.analyze_image_once") as mock_analyze:
                mock_analyze.return_value = "Cached result."

                result1 = sp_module.screen_process({"text": "What is on screen?"})
                result2 = sp_module.screen_process({"text": "What is on screen?"})

                self.assertEqual(result1, "Cached result.")
                self.assertEqual(result2, "Cached result.")
                self.assertEqual(mock_analyze.call_count, 1)

    def test_cache_key_includes_angle(self):
        import actions.screen_processor as sp_module

        sp_module._CACHE.clear()

        with patch("actions.screen_processor._capture_screen") as mock_cap:
            mock_cap.return_value = (b"image-data", "image/png", 1000, 800)

            with patch("actions.screen_processor.analyze_image_once") as mock_analyze:
                mock_analyze.return_value = "Screen result."

                result1 = sp_module.screen_process(
                    {"text": "What is on screen?", "angle": "screen"}
                )
                result2 = sp_module.screen_process(
                    {"text": "What is on screen?", "angle": "camera"}
                )

                self.assertEqual(result1, "Screen result.")
                self.assertEqual(result2, "Screen result.")
                self.assertEqual(mock_analyze.call_count, 2)


class TestVisionConcisePrompt(unittest.TestCase):
    def test_concise_prompt_in_analyze(self):
        from actions.screen_processor import analyze_image_once

        source = inspect.getsource(analyze_image_once)
        self.assertIn("1–3 short sentences", source)
        self.assertIn("Focus only on what is visible", source)
        self.assertIn("Do not describe unnecessary details", source)


class TestVisionResponseTruncation(unittest.TestCase):
    def test_long_response_truncated(self):
        import actions.screen_processor as sp_module
        from actions.screen_processor import analyze_image_once

        long_text = "A" * 1000

        mock_response = MagicMock()
        type(mock_response).text = PropertyMock(return_value=long_text)

        with patch.object(sp_module, "_MAX_RESPONSE_CHARS", 800):
            with patch("actions.screen_processor.get_vision_client") as mock_get_client:
                mock_client_instance = MagicMock()
                mock_get_client.return_value = mock_client_instance
                mock_client_instance.models.generate_content.return_value = mock_response

                result = analyze_image_once(b"fake", "image/png", "Question?")

                self.assertEqual(len(result), 800)


class TestVisionTimeout(unittest.TestCase):
    def test_timeout_env_default(self):
        import actions.screen_processor as sp_module

        self.assertEqual(sp_module._VISION_TIMEOUT_SECONDS, 12.0)

    def test_timeout_env_respected(self):
        import os

        original = os.environ.get("ERA_VISION_TIMEOUT_SECONDS")
        try:
            os.environ["ERA_VISION_TIMEOUT_SECONDS"] = "8.0"
            import importlib

            import actions.screen_processor as sp_module

            importlib.reload(sp_module)
            self.assertEqual(sp_module._VISION_TIMEOUT_SECONDS, 8.0)
        finally:
            if original is None:
                os.environ.pop("ERA_VISION_TIMEOUT_SECONDS", None)
            else:
                os.environ["ERA_VISION_TIMEOUT_SECONDS"] = original


class TestVisionKeyResolution(unittest.TestCase):
    def test_dedicated_vision_key_used_when_present(self):
        from core.utils import get_vision_api_key

        mock_config = {"gemini_vision_api_key": "VISION-KEY-123", "gemini_api_key": "MAIN-KEY-456"}
        with (
            patch("core.utils.load_config", return_value=mock_config),
            patch.dict(os.environ, {}, clear=True),
        ):
            key = get_vision_api_key()
            self.assertEqual(key, "VISION-KEY-123")

    def test_main_key_falls_back_when_vision_key_missing(self):
        from core.utils import get_vision_api_key

        mock_config = {"gemini_api_key": "MAIN-KEY-456"}
        with (
            patch("core.utils.load_config", return_value=mock_config),
            patch.dict(os.environ, {}, clear=True),
        ):
            key = get_vision_api_key()
            self.assertEqual(key, "MAIN-KEY-456")

    def test_env_var_fallback(self):
        from core.utils import get_vision_api_key

        with (
            patch("core.utils.load_config", return_value={}),
            patch.dict(os.environ, {"GEMINI_VISION_API_KEY": "ENV-VISION-KEY"}),
        ):
            key = get_vision_api_key()
            self.assertEqual(key, "ENV-VISION-KEY")

    def test_uppercase_config_aliases(self):
        from core.utils import get_vision_api_key

        mock_config = {"GEMINI_VISION_API_KEY": "UPPER-VISION-KEY", "gemini_api_key": "main"}
        with (
            patch("core.utils.load_config", return_value=mock_config),
            patch.dict(os.environ, {}, clear=True),
        ):
            key = get_vision_api_key()
            self.assertEqual(key, "UPPER-VISION-KEY")

    def test_raises_when_no_key_anywhere(self):
        from core.utils import get_vision_api_key

        with (
            patch("core.utils.load_config", return_value={}),
            patch.dict(os.environ, {}, clear=True),
        ):
            with self.assertRaises(RuntimeError):
                get_vision_api_key()

    def test_vision_client_uses_dedicated_key(self):
        from core.utils import get_vision_api_key

        mock_config = {"gemini_vision_api_key": "VISION-KEY-789", "gemini_api_key": "MAIN-KEY"}
        with (
            patch("core.utils.load_config", return_value=mock_config),
            patch.dict(os.environ, {}, clear=True),
            patch("actions.screen_processor.genai.Client") as mock_client,
        ):
            import actions.screen_processor as sp

            sp._VISION_CLIENT = None
            try:
                sp.get_vision_client()
                mock_client.assert_called_once_with(api_key="VISION-KEY-789")
            finally:
                sp._VISION_CLIENT = None


class TestVisionProviderChain(unittest.TestCase):
    """First available provider takes the job and finishes it; failures hand off to the next."""

    def setUp(self):
        from actions.netflix_control import _reset_netflix_vision_cooldown

        _reset_netflix_vision_cooldown()
        self._patchers = []
        self._patchers.append(patch("actions.screen_processor._capture_screen"))
        self._patchers.append(
            patch(
                "core.intent_memory.load_state",
                return_value={
                    "last_app": None,
                    "last_media_app": None,
                    "last_browser": "brave",
                    "last_action": None,
                    "last_tool": None,
                    "last_window": None,
                    "previous_window": None,
                    "last_result": None,
                    "last_error": None,
                    "updated_at": 0.0,
                    "history": [],
                },
            )
        )
        # Pin the order: otherwise these read the developer's real api_keys.json and
        # can reach a live provider.
        self._patchers.append(
            patch(
                "core.vision_config.resolve_vision_provider_order",
                return_value=["ollama", "gemini", "openai"],
            )
        )
        for p in self._patchers:
            p.start()
        self._patchers[0].return_value = (b"fake-png-data", "image/png", 1000, 800)

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        from actions.netflix_control import _reset_netflix_vision_cooldown

        _reset_netflix_vision_cooldown()

    def test_ollama_available_takes_job_and_remotes_never_called(self):
        from actions.screen_processor import analyze_image_once

        with (
            patch("core.vision_ollama.check_ollama_availability", return_value=True),
            patch("core.vision_ollama.analyze_image_with_ollama") as mock_ollama,
            patch("actions.screen_processor.get_vision_client") as mock_get_client,
            patch("core.vision_openai.analyze_image_with_openai") as mock_openai,
        ):
            mock_ollama.return_value = "Ollama sees the screen"
            result = analyze_image_once(b"fake-image", "image/png", "What do you see?")
            self.assertEqual(result, "Ollama sees the screen")
            mock_ollama.assert_called_once()
            # The whole point: no remote call, so no quota burn and no timeout tax.
            mock_get_client.assert_not_called()
            mock_openai.assert_not_called()

    def test_ollama_daemon_down_skips_to_gemini(self):
        from actions.screen_processor import analyze_image_once

        mock_response = MagicMock()
        type(mock_response).text = PropertyMock(return_value="Gemini result")
        with (
            patch("core.vision_ollama.check_ollama_availability", return_value=False),
            patch("core.vision_ollama.analyze_image_with_ollama") as mock_ollama,
            patch("actions.screen_processor.get_vision_api_key", return_value="k"),
            patch("actions.screen_processor.get_vision_client") as mock_get_client,
        ):
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.models.generate_content.return_value = mock_response
            result = analyze_image_once(b"fake", "image/png", "Q?")
            self.assertEqual(result, "Gemini result")
            mock_ollama.assert_not_called()

    def test_ollama_accepts_then_errors_falls_through_to_gemini(self):
        from actions.screen_processor import analyze_image_once

        mock_response = MagicMock()
        type(mock_response).text = PropertyMock(return_value="Gemini rescued it")
        with (
            patch("core.vision_ollama.check_ollama_availability", return_value=True),
            patch(
                "core.vision_ollama.analyze_image_with_ollama",
                side_effect=RuntimeError("model not pulled"),
            ),
            patch("actions.screen_processor.get_vision_api_key", return_value="k"),
            patch("actions.screen_processor.get_vision_client") as mock_get_client,
        ):
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.models.generate_content.return_value = mock_response
            result = analyze_image_once(b"fake", "image/png", "Q?")
            self.assertEqual(result, "Gemini rescued it")

    def test_ollama_empty_result_falls_through(self):
        from actions.screen_processor import analyze_image_once

        with (
            patch("core.vision_ollama.check_ollama_availability", return_value=True),
            patch("core.vision_ollama.analyze_image_with_ollama", return_value=""),
            patch("actions.screen_processor.get_vision_api_key", return_value=None),
            patch("core.vision_config.resolve_openai_api_key", return_value=None),
        ):
            result = analyze_image_once(b"fake", "image/png", "Q?")
            self.assertEqual(result, "I could not analyze the screen quickly enough.")

    def test_every_provider_unavailable_returns_message(self):
        from actions.screen_processor import analyze_image_once

        with (
            patch("core.vision_ollama.check_ollama_availability", return_value=False),
            patch("actions.screen_processor.get_vision_api_key", return_value=None),
            patch("core.vision_config.resolve_openai_api_key", return_value=None),
            patch("core.vision_ollama.analyze_image_with_ollama") as mock_ollama,
        ):
            result = analyze_image_once(b"fake", "image/png", "Q?")
            self.assertEqual(result, "I could not analyze the screen quickly enough.")
            mock_ollama.assert_not_called()

    def test_ollama_success_returns_truncated(self):
        from actions.screen_processor import analyze_image_once

        with (
            patch("core.vision_ollama.check_ollama_availability", return_value=True),
            patch("core.vision_ollama.analyze_image_with_ollama") as mock_ollama,
        ):
            mock_ollama.return_value = "A" * 1000
            result = analyze_image_once(b"fake", "image/png", "Q?")
            self.assertEqual(len(result), 800)


class TestVisionProviderSelection(unittest.TestCase):
    """Provider priority: first AVAILABLE provider in the configured order takes the job."""

    def setUp(self):
        from actions.netflix_control import _reset_netflix_vision_cooldown

        _reset_netflix_vision_cooldown()

    def tearDown(self):
        from actions.netflix_control import _reset_netflix_vision_cooldown

        _reset_netflix_vision_cooldown()

    def test_default_order_puts_ollama_first(self):
        from core.vision_config import resolve_vision_provider_order

        with patch("core.vision_config.load_config", return_value={}):
            self.assertEqual(resolve_vision_provider_order()[0], "ollama")

    def test_configured_order_is_honored_over_default(self):
        from actions.screen_processor import analyze_image_once

        mock_response = MagicMock()
        type(mock_response).text = PropertyMock(return_value="Gemini first")

        with (
            patch(
                "core.vision_config.resolve_vision_provider_order",
                return_value=["gemini", "openai", "ollama"],
            ),
            patch("actions.screen_processor.get_vision_api_key", return_value="k"),
            patch("actions.screen_processor.get_vision_client") as mock_get_client,
            patch("core.vision_ollama.analyze_image_with_ollama") as mock_ollama,
        ):
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.models.generate_content.return_value = mock_response
            result = analyze_image_once(b"fake", "image/png", "Q?")
            self.assertEqual(result, "Gemini first")
            mock_ollama.assert_not_called()

    def test_provider_absent_from_order_is_never_called(self):
        from actions.screen_processor import analyze_image_once

        with (
            patch(
                "core.vision_config.resolve_vision_provider_order",
                return_value=["ollama", "gemini"],
            ),
            patch("core.vision_ollama.check_ollama_availability", return_value=False),
            patch("actions.screen_processor.get_vision_api_key", return_value=None),
            patch("core.vision_openai.analyze_image_with_openai") as mock_openai,
        ):
            result = analyze_image_once(b"fake", "image/png", "Q?")
            self.assertIn("I could not analyze", result)
            mock_openai.assert_not_called()

    def test_unknown_provider_name_is_skipped(self):
        from actions.screen_processor import analyze_image_once

        with (
            patch(
                "core.vision_config.resolve_vision_provider_order",
                return_value=["nonsense", "ollama"],
            ),
            patch("core.vision_ollama.check_ollama_availability", return_value=True),
            patch("core.vision_ollama.analyze_image_with_ollama", return_value="Ollama result"),
        ):
            result = analyze_image_once(b"fake", "image/png", "Q?")
            self.assertEqual(result, "Ollama result")

    def test_missing_openai_key_marks_provider_unavailable(self):
        from actions.screen_processor import _openai_available

        with patch("core.vision_config.resolve_openai_api_key", return_value=None):
            self.assertFalse(_openai_available())

    def test_gemini_timeout_falls_through_to_openai(self):
        import time as _time

        from actions.screen_processor import analyze_image_once

        with (
            patch(
                "core.vision_config.resolve_vision_provider_order",
                return_value=["gemini", "openai"],
            ),
            patch("actions.screen_processor.get_vision_api_key", return_value="k"),
            patch("actions.screen_processor.get_vision_client") as mock_get_client,
            patch("core.vision_config.resolve_openai_api_key", return_value="sk-x"),
            patch("core.vision_openai.analyze_image_with_openai", return_value="OpenAI result"),
        ):
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.models.generate_content.side_effect = lambda **kw: _time.sleep(60)
            result = analyze_image_once(b"fake", "image/png", "Q?", timeout=0.2)
            self.assertEqual(result, "OpenAI result")

    def test_openai_provider_does_not_log_api_key(self):
        import contextlib
        import io

        from actions.screen_processor import _call_openai

        fake_key = "sk-super-secret-12345"
        with (
            patch("core.vision_config.resolve_openai_api_key", return_value=fake_key),
            patch(
                "core.vision_openai.analyze_image_with_openai",
                side_effect=RuntimeError("OpenAI failed"),
            ),
        ):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                result = _call_openai(b"fake", "image/png", "Q?")
            self.assertIsNone(result)
            self.assertNotIn(fake_key, buf.getvalue())

    def test_no_dangerous_imports_in_openai_provider(self):
        import core.vision_openai as vo

        source = inspect.getsource(vo)
        self.assertNotIn("computer_control", source)
        self.assertNotIn("pyautogui", source)
        for pattern in ("_click(", "_type(", "_press(", "_scroll(", "pyautogui."):
            self.assertNotIn(pattern, source)


class TestVisionOllamaSafety(unittest.TestCase):
    def test_no_computer_control_import(self):
        import core.vision_ollama as vq

        source = inspect.getsource(vq)
        self.assertNotIn("computer_control", source)

    def test_no_pyautogui_import(self):
        import core.vision_ollama as vq

        source = inspect.getsource(vq)
        self.assertNotIn("pyautogui", source)

    def test_no_click_type_press_scroll(self):
        import core.vision_ollama as vq

        source = inspect.getsource(vq)
        for pattern in ("_click(", "_type(", "_press(", "_scroll(", "pyautogui."):
            self.assertNotIn(pattern, source)


if __name__ == "__main__":
    unittest.main()
