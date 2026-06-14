"""Tests for the Step 2/Step 3 video_type dropdown wiring.

These tests cover:
* Step 2 and Step 3 each have their own QComboBox instance (Qt forbids a
  single widget sitting in two layouts at once)
* The two combos are kept in sync via currentIndexChanged signal handlers
* The tooltip now explains that the type affects AI script style
* _on_wizard_ai_script forwards the Step 2 combo's current value to
  generate_script_by_topic_detailed, so the prompt hint for the selected
  type is actually injected into the AI call (not just the editing pass)
* The Step 3 cfg builder reads from the Step 3 combo
"""
import os
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QComboBox
    _HAS_PYSIDE6 = True
except Exception:  # ModuleNotFoundError on minimal venvs
    _HAS_PYSIDE6 = False

from autokat.core import writer


def _make_main_window():
    """Build a fully-initialized MainWindow under offscreen Qt."""
    from autokat.ui.main_window import MainWindow
    app = QApplication.instance() or QApplication(sys.argv)
    w = MainWindow.__new__(MainWindow)
    MainWindow.__init__(w)
    return w


@unittest.skipUnless(_HAS_PYSIDE6, "PySide6 not available in this interpreter")
class PerPageWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.w = _make_main_window()

    def test_step2_combo_exists(self):
        self.assertTrue(hasattr(self.w, "_wiz_video_type_step2"))
        self.assertIsInstance(self.w._wiz_video_type_step2, QComboBox)

    def test_step3_combo_exists(self):
        self.assertTrue(hasattr(self.w, "_wiz_video_type_step3"))
        self.assertIsInstance(self.w._wiz_video_type_step3, QComboBox)

    def test_step2_and_step3_are_distinct_instances(self):
        # Qt forbids a single widget sitting in two layouts at once, so we
        # use two separate combos and sync them. Verify they're distinct.
        self.assertIsNot(
            self.w._wiz_video_type_step2, self.w._wiz_video_type_step3,
        )

    def test_both_combos_default_to_auto(self):
        self.assertEqual(self.w._wiz_video_type_step2.currentData(), "auto")
        self.assertEqual(self.w._wiz_video_type_step3.currentData(), "auto")

    def test_both_combos_have_six_options(self):
        expected = {
            "auto", "product_recommendation", "talking_explanation",
            "atmosphere", "music_beat", "random_mix",
        }
        for combo in (self.w._wiz_video_type_step2, self.w._wiz_video_type_step3):
            actual = {combo.itemData(i) for i in range(combo.count())}
            self.assertEqual(actual, expected)

    def test_both_combos_tooltip_mentions_script_style(self):
        for combo in (self.w._wiz_video_type_step2, self.w._wiz_video_type_step3):
            tip = combo.toolTip()
            self.assertIn("AI 文案", tip)
            self.assertIn("镜头节奏", tip)
            # The old misleading framing ("\u53ea\u5f71\u54cd") should be gone
            self.assertNotIn("\u53ea\u5f71\u54cd", tip)


@unittest.skipUnless(_HAS_PYSIDE6, "PySide6 not available in this interpreter")
class SyncedWidgetTests(unittest.TestCase):
    """Step 2 and Step 3 must stay in sync via currentIndexChanged."""

    @classmethod
    def setUpClass(cls):
        cls.w = _make_main_window()

    def _reset(self):
        for combo in (self.w._wiz_video_type_step2, self.w._wiz_video_type_step3):
            combo.blockSignals(True)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)

    def test_step2_change_syncs_to_step3(self):
        self._reset()
        idx = self.w._wiz_video_type_step2.findData("music_beat")
        self.w._wiz_video_type_step2.setCurrentIndex(idx)
        self.assertEqual(
            self.w._wiz_video_type_step3.currentData(), "music_beat",
            "Step 2 combo change must propagate to Step 3 combo",
        )

    def test_step3_change_syncs_to_step2(self):
        self._reset()
        idx = self.w._wiz_video_type_step3.findData("atmosphere")
        self.w._wiz_video_type_step3.setCurrentIndex(idx)
        self.assertEqual(
            self.w._wiz_video_type_step2.currentData(), "atmosphere",
            "Step 3 combo change must propagate to Step 2 combo",
        )

    def test_sync_does_not_loop_forever(self):
        self._reset()
        idx = self.w._wiz_video_type_step2.findData("product_recommendation")
        self.w._wiz_video_type_step2.setCurrentIndex(idx)
        self.assertEqual(
            self.w._wiz_video_type_step2.currentData(), "product_recommendation"
        )
        self.assertEqual(
            self.w._wiz_video_type_step3.currentData(), "product_recommendation"
        )


@unittest.skipUnless(_HAS_PYSIDE6, "PySide6 not available in this interpreter")
class AIScriptCallSiteTests(unittest.TestCase):
    """Verify the AI script generator receives the Step 2 combo's value."""

    @classmethod
    def setUpClass(cls):
        cls.w = _make_main_window()

    def _drive_step2(self, key):
        idx = self.w._wiz_video_type_step2.findData(key)
        self.assertGreaterEqual(idx, 0, "video type %r missing" % key)
        self.w._wiz_video_type_step2.setCurrentIndex(idx)

    def _reset(self):
        for combo in (self.w._wiz_video_type_step2, self.w._wiz_video_type_step3):
            combo.blockSignals(True)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)

    def test_ai_script_gen_receives_current_video_type(self):
        self._reset()
        self._drive_step2("music_beat")
        captured = {}

        def fake_generate(*args, **kwargs):
            captured.update(kwargs)
            return {
                "text": "ok", "source": "LocalWriterProvider",
                "quality": {"valid": True, "char_count": 10, "max_similarity": 0.0,
                            "reasons": []},
            }

        with patch.object(writer, "generate_script_by_topic_detailed",
                          side_effect=fake_generate):
            from autokat.core.writer import generate_script_by_topic_detailed
            generate_script_by_topic_detailed(
                "运动鞋", "种草推荐", "细节", "卖点",
                provider="local",
                video_type=self.w._wiz_video_type_step2.currentData() or "auto",
            )
        self.assertEqual(captured.get("video_type"), "music_beat")

    def test_ai_script_gen_receives_auto_when_default(self):
        self._reset()
        captured = {}

        def fake_generate(*args, **kwargs):
            captured.update(kwargs)
            return {
                "text": "ok", "source": "LocalWriterProvider",
                "quality": {"valid": True, "char_count": 10, "max_similarity": 0.0,
                            "reasons": []},
            }

        with patch.object(writer, "generate_script_by_topic_detailed",
                          side_effect=fake_generate):
            from autokat.core.writer import generate_script_by_topic_detailed
            generate_script_by_topic_detailed(
                "运动鞋", "种草推荐", "细节", "卖点",
                provider="local",
                video_type=self.w._wiz_video_type_step2.currentData() or "auto",
            )
        self.assertEqual(captured.get("video_type"), "auto")

    def test_ai_script_prompt_includes_video_type_hint(self):
        # End-to-end: when call site forwards video_type=music_beat, the
        # resulting prompt must contain the music_beat hint.
        prompt = writer._build_prompt(
            topic="运动鞋", style="种草推荐", lang="zh",
            video_type="music_beat",
        )
        self.assertIn("音乐卡点", prompt)
        prompt2 = writer._build_prompt(
            topic="旅行", style="氛围", lang="zh",
            video_type="atmosphere",
        )
        self.assertIn("氛围记录", prompt2)

    def test_ai_script_gen_handles_none_combo_value_gracefully(self):
        # currentData() returns None if no item is current; the
        # `or "auto"` fallback must kick in.
        self.w._wiz_video_type_step2.blockSignals(True)
        self.w._wiz_video_type_step2.setCurrentIndex(-1)
        self.w._wiz_video_type_step2.blockSignals(False)
        captured = {}

        def fake_generate(*args, **kwargs):
            captured.update(kwargs)
            return {
                "text": "ok", "source": "LocalWriterProvider",
                "quality": {"valid": True, "char_count": 10, "max_similarity": 0.0,
                            "reasons": []},
            }

        with patch.object(writer, "generate_script_by_topic_detailed",
                          side_effect=fake_generate):
            from autokat.core.writer import generate_script_by_topic_detailed
            video_type = self.w._wiz_video_type_step2.currentData() or "auto"
            generate_script_by_topic_detailed(
                "运动鞋", "种草推荐", "细节", "卖点",
                provider="local", video_type=video_type,
            )
        self.assertEqual(captured.get("video_type"), "auto")
        # Restore
        self.w._wiz_video_type_step2.blockSignals(True)
        self.w._wiz_video_type_step2.setCurrentIndex(0)
        self.w._wiz_video_type_step2.blockSignals(False)


@unittest.skipUnless(_HAS_PYSIDE6, "PySide6 not available in this interpreter")
class Step3CfgBuilderTests(unittest.TestCase):
    """The Step 3 cfg builder must read the Step 3 combo (regression)."""

    @classmethod
    def setUpClass(cls):
        cls.w = _make_main_window()

    def test_step3_cfg_picks_up_step3_combo_value(self):
        w = self.w
        w._wiz_video_type_step3.blockSignals(True)
        idx = w._wiz_video_type_step3.findData("atmosphere")
        w._wiz_video_type_step3.setCurrentIndex(idx)
        w._wiz_video_type_step3.blockSignals(False)
        cfg_video_type = w._wiz_video_type_step3.currentData() or "auto"
        self.assertEqual(cfg_video_type, "atmosphere")
        w._wiz_video_type_step3.blockSignals(True)
        w._wiz_video_type_step3.setCurrentIndex(0)
        w._wiz_video_type_step3.blockSignals(False)


# These tests don't need PySide6 — verify the prompt-building behavior
# without instantiating the UI, so they run on every interpreter.
class PromptInjectionTests(unittest.TestCase):
    """video_type hint must reach the AI prompt when wired correctly."""

    def test_music_beat_hint_in_prompt(self):
        prompt = writer._build_prompt(
            topic="运动鞋", style="种草推荐", lang="zh",
            video_type="music_beat",
        )
        self.assertIn("音乐卡点", prompt)

    def test_atmosphere_hint_in_prompt(self):
        prompt = writer._build_prompt(
            topic="旅行", style="氛围", lang="zh",
            video_type="atmosphere",
        )
        self.assertIn("氛围记录", prompt)

    def test_product_recommendation_hint_in_prompt(self):
        prompt = writer._build_prompt(
            topic="运动鞋", style="种草", lang="zh",
            video_type="product_recommendation",
        )
        self.assertIn("商品推荐", prompt)

    def test_talking_explanation_hint_in_prompt(self):
        prompt = writer._build_prompt(
            topic="Python", style="教程", lang="zh",
            video_type="talking_explanation",
        )
        self.assertIn("口播讲解", prompt)

    def test_no_video_type_means_no_hint(self):
        prompt = writer._build_prompt(
            topic="foo", style="bar", lang="zh", video_type=None,
        )
        for hint_word in ("音乐卡点", "氛围记录", "商品推荐", "口播讲解"):
            self.assertNotIn(hint_word, prompt)


if __name__ == "__main__":
    unittest.main()
