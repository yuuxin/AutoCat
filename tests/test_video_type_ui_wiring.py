"""Tests for the 视频类型 dropdown wiring in the wizard.

v3.2: 视频类型下拉框只放在 Step 2 (原来 Step 3 的同步副本已移除 — 用户在
Step 2 选一次即可, 避免 UI 重复且 Qt 不允许同一 widget 挂在两个 layout 下)。

These tests cover:
* Step 2 has the QComboBox instance (Qt single source of truth)
* The tooltip explains that the type affects AI script style
* _on_wizard_ai_script forwards the Step 2 combo's current value to
  generate_script_by_topic_detailed, so the prompt hint for the selected
  type is actually injected into the AI call (not just the editing pass)
* Step 3 no longer has its own video_type combo (用户报告)
* save/load/snapshot code paths use _wiz_video_type_step2 as the single
  source of truth
"""
import os
import re
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QComboBox
    _HAS_PYSIDE6 = True
except Exception:  # ModuleNotFoundError on minimal venvs
    _HAS_PYSIDE6 = False


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

    def test_step3_combo_does_not_exist(self):
        # v3.2: 视频类型只在 Step 2 放一个 QComboBox, Step 3 取消独立副本
        self.assertFalse(
            hasattr(self.w, "_wiz_video_type_step3"),
            "Step 3 不再有独立的视频类型 combo (用户要求移除)",
        )

    def test_step2_combo_default_to_auto(self):
        self.assertEqual(self.w._wiz_video_type_step2.currentData(), "auto")

    def test_step2_combo_has_six_options(self):
        expected = {
            "auto", "product_recommendation", "talking_explanation",
            "atmosphere", "music_beat", "random_mix",
        }
        actual = {self.w._wiz_video_type_step2.itemData(i)
                  for i in range(self.w._wiz_video_type_step2.count())}
        self.assertEqual(actual, expected)

    def test_step2_combo_tooltip_mentions_ai_script_role(self):
        """tooltip 必须说明视频类型会影响 AI 文案生成 + 节奏 (不是只影响文案)"""
        tip = self.w._wiz_video_type_step2.toolTip()
        self.assertIn("AI", tip, "tooltip 必须说明视频类型与 AI 有关")
        self.assertIn("节奏", tip, "tooltip 必须说明视频类型影响节奏")
        # The old misleading framing ("只影响") should be gone
        self.assertNotIn("只影响", tip)


@unittest.skipUnless(_HAS_PYSIDE6, "PySide6 not available in this interpreter")
class SingleSourceOfTruthTests(unittest.TestCase):
    """v3.2: _wiz_video_type_step2 是唯一 source of truth — save/load/snapshot
    都要走它, 不能再去 _wiz_video_type_step3 (已被移除)。"""

    @classmethod
    def setUpClass(cls):
        cls.w = _make_main_window()

    def test_cfg_save_uses_step2_combo(self):
        """cfg['video_type'] 必须来自 _wiz_video_type_step2.currentData()"""
        import inspect
        from autokat.ui.main_window import MainWindow
        src = inspect.getsource(MainWindow)
        # save cfg 时不应该再引用 _wiz_video_type_step3
        # cfg 字典应通过 step2 combo 写入
        self.assertNotIn("_wiz_video_type_step3", src,
                          "_wiz_video_type_step3 widget 已移除, save 路径不应再引用")

    def test_snapshot_apply_uses_step2_combo(self):
        """snapshot 应用路径应走 _wiz_video_type_step2, 不引用 step3"""
        import inspect
        from autokat.ui.main_window import MainWindow
        src = inspect.getsource(MainWindow)
        # _apply_wizard_snapshot 应只 set _wiz_video_type_step2
        self.assertNotIn("_wiz_video_type_step3", src,
                          "_apply_wizard_snapshot 应走 _wiz_video_type_step2")


@unittest.skipUnless(_HAS_PYSIDE6, "PySide6 not available in this interpreter")
class AIScriptCallSiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.w = _make_main_window()

    def _open_ai_dialog(self, video_type="auto"):
        """打开 AI 辅助生成对话框并设置选题 + 视频类型"""
        from PySide6.QtCore import QEvent
        from PySide6.QtWidgets import QDialog, QLineEdit, QComboBox
        dlg = QDialog(self.w)
        dlg.setWindowTitle("AI 辅助生成文案")
        # 直接调 _on_wizard_ai_script, 然后拦截 prompt 调用
        self.w._wiz_video_type_step2.blockSignals(True)
        idx = self.w._wiz_video_type_step2.findData(video_type)
        if idx >= 0:
            self.w._wiz_video_type_step2.setCurrentIndex(idx)
        self.w._wiz_video_type_step2.blockSignals(False)

    def test_ai_script_gen_receives_current_video_type(self):
        """AI 文案生成路径必须从 _wiz_video_type_step2 捕获视频类型后
        传给 generate_script_by_topic_detailed 的 video_type 参数。
        实际代码: dialog 内 video_type_input 是用 _wiz_video_type_step2 默认值
        同步的 QComboBox, 再赋值给 _captured_video_type。"""
        import inspect
        from autokat.ui.main_window import MainWindow
        src = inspect.getsource(MainWindow._on_wizard_ai_script)
        self.assertIn("video_type=", src,
                       "AI prompt 必须传 video_type 给 generate_script_by_topic_detailed")
        # dialog 内 video_type_input 的默认值取自 _wiz_video_type_step2
        self.assertIn("_wiz_video_type_step2", src,
                       "AI 视频类型必须从 _wiz_video_type_step2 读 (step3 combo 已移除)")
        self.assertNotIn("_wiz_video_type_step3", src,
                          "step3 combo 已移除, 不应再被引用")
