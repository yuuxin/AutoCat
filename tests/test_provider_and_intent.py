"""Tests for the AI provider factory, video_type injection, INTENT_VERSION,
and _call_deepseek_api explicit kwargs added during the six-stage refactor."""
import json
import os
import unittest
from unittest.mock import patch

from autokat.core import ai_providers, editor, writer


class BuildWriterProviderTests(unittest.TestCase):
    def test_local_factory_returns_local_provider(self):
        provider = ai_providers.build_writer_provider("local")
        self.assertIsInstance(provider, ai_providers.LocalWriterProvider)

    def test_unknown_provider_raises_value_error(self):
        with self.assertRaises(ValueError):
            ai_providers.build_writer_provider("gpt-4")

    def test_deepseek_without_keychain_raises_runtime_error(self):
        with patch.object(ai_providers, "load_deepseek_key", return_value=""):
            with self.assertRaises(RuntimeError):
                ai_providers.build_writer_provider("deepseek")

    def test_deepseek_with_key_uses_ai_settings(self):
        with patch.object(ai_providers, "load_deepseek_key", return_value="k"), \
             patch.object(ai_providers, "load_ai_settings",
                          return_value={"deepseek_url": "https://x", "deepseek_model": "m"}):
            provider = ai_providers.build_writer_provider("deepseek")
            self.assertEqual(provider.api_key, "k")
            self.assertEqual(provider.api_url, "https://x")
            self.assertEqual(provider.model, "m")


class VideoTypePromptHintTests(unittest.TestCase):
    def test_every_supported_type_returns_specific_hint(self):
        for key in ("product_recommendation", "talking_explanation",
                    "atmosphere", "music_beat", "random_mix"):
            hint = ai_providers.video_type_prompt_hint(key)
            self.assertIn("视频类型", hint)
            self.assertNotIn("由你根据选题自由判断", hint)

    def test_auto_and_unknown_fall_back_to_auto_hint(self):
        for key in ("auto", None, "", "novel_type"):
            hint = ai_providers.video_type_prompt_hint(key)
            self.assertIn("由你根据选题自由判断", hint)


class DeepSeekCallKwargsTests(unittest.TestCase):
    def test_call_uses_explicit_kwargs_when_provided(self):
        captured = {}

        class _Resp:
            def __enter__(self_inner):
                return self_inner
            def __exit__(self_inner, *a):
                return False
            def read(self_inner):
                return b'{"choices": [{"message": {"content": "ok"}}]}'

        def fake_urlopen(req, timeout=30):
            captured["body"] = req.data
            captured["url"] = req.full_url
            captured["auth"] = req.headers.get("Authorization")
            return _Resp()

        import urllib.request as urllib_req
        with patch.object(urllib_req, "urlopen", fake_urlopen):
            result = writer._call_deepseek_api(
                "hello", max_tokens=64,
                api_key="custom-key",
                api_url="https://api.example.com/v1/chat/completions",
                model="custom-model",
            )
        self.assertEqual(result, "ok")
        self.assertEqual(captured["url"], "https://api.example.com/v1/chat/completions")
        self.assertEqual(captured["auth"], "Bearer custom-key")
        body = json.loads(captured["body"].decode())
        self.assertEqual(body["model"], "custom-model")


class MigrateEnvKeyTests(unittest.TestCase):
    def setUp(self):
        self._orig = os.environ.pop("DEEPSEEK_API_KEY", None)

    def tearDown(self):
        os.environ.pop("DEEPSEEK_API_KEY", None)
        if self._orig is not None:
            os.environ["DEEPSEEK_API_KEY"] = self._orig

    def test_migrate_moves_env_to_keychain(self):
        saved = []

        def fake_save(key):
            saved.append(key)

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "old-key"}), \
             patch.object(ai_providers, "save_deepseek_key", side_effect=fake_save), \
             patch.object(ai_providers, "load_deepseek_key", return_value=""):
            migrated = ai_providers.migrate_env_key_to_keychain()
        self.assertTrue(migrated)
        self.assertEqual(saved, ["old-key"])
        self.assertNotIn("DEEPSEEK_API_KEY", os.environ)

    def test_migrate_is_idempotent_when_keychain_already_has_same_key(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "old-key"}), \
             patch.object(ai_providers, "save_deepseek_key") as save, \
             patch.object(ai_providers, "load_deepseek_key", return_value="old-key"):
            migrated = ai_providers.migrate_env_key_to_keychain()
        self.assertFalse(migrated)
        save.assert_not_called()
        self.assertNotIn("DEEPSEEK_API_KEY", os.environ)


class IntentVersionTests(unittest.TestCase):
    def test_intent_version_constant_is_set(self):
        self.assertEqual(editor.INTENT_VERSION, "intent-v1")

    def test_generate_script_writes_intent_version_into_plan(self):
        sentences = [{"start": 0.0, "end": 1.0, "text": "hi"}]
        pool = [{"id": 1, "path": "/tmp/x.mp4", "duration": 5.0,
                 "width": 1080, "height": 1920, "type": "video",
                 "source_id": 1, "tags": [], "capability_summary": ""}]
        script = editor.generate_script(
            sentences, material_pool=pool,
            config={"video_type": "atmosphere",
                    "transition_duration": 0.3, "fps": 30,
                    "narration_text": "hi"},
        )
        self.assertEqual(script.get("intent_version"), editor.INTENT_VERSION)
        self.assertEqual(script.get("video_type"), "atmosphere")


class BuildPromptVideoTypeTests(unittest.TestCase):
    def test_prompt_carries_video_type_hint(self):
        prompt = writer._build_prompt(
            topic="运动鞋", style="种草推荐", lang="zh", video_type="music_beat",
        )
        self.assertIn("音乐卡点", prompt)
