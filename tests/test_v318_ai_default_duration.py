"""v3.18 守护测试: AI 辅助文案默认时长 15-30s → 20-30s.

用户反馈: 15s 太短支撑不了产品介绍, 短视频主流是 20s+ 的中长种草.
默认 20-30s → 估字 85-156, 既能讲清产品又不至于太长.

测试策略: UI 默认值是 QSpinBox.setValue(N) 调用的字面常量, 走
inspect.getsource 读 main_window.py 源码, 断言默认值是 20 (不是 15).
避免实例化整个 QDialog (依赖 PySide6 + 全部 autokat.ui 子模块).
"""
import re
import unittest
import inspect

from autokat.core.writer import estimate_chars_for_duration_range
from autokat.ui import main_window as mw


class AIAssistDefaultDurationTests(unittest.TestCase):
    """v3.18: AI 辅助生成文案对话框, 时长范围默认应为 20-30 秒."""

    def _on_wizard_ai_script_src(self) -> str:
        # 拿到 _on_wizard_ai_script 函数的源码 (含 dur_min / dur_max setValue)
        return inspect.getsource(mw.MainWindow._on_wizard_ai_script)

    def test_default_min_duration_is_20s(self):
        """v3.18: dur_min.setValue 必须是 20, 不是 15 (旧默认值)."""
        src = self._on_wizard_ai_script_src()
        # 直接抓 dur_min.setValue 后的数字
        m = re.search(r"dur_min\.setValue\((\d+)\)", src)
        self.assertIsNotNone(m, "v3.18: dur_min.setValue(N) 必须在 _on_wizard_ai_script 中")
        n = int(m.group(1))
        self.assertEqual(n, 20,
            f"v3.18: AI 辅助文案默认最短时长应为 20s, got {n}s "
            f"(旧版是 15s, 用户反馈太短)")

    def test_default_max_duration_unchanged_30s(self):
        """v3.18: dur_max.setValue 仍应为 30s (max 不变, 只调 min)."""
        src = self._on_wizard_ai_script_src()
        m = re.search(r"dur_max\.setValue\((\d+)\)", src)
        self.assertIsNotNone(m, "v3.18: dur_max.setValue(N) 必须在 _on_wizard_ai_script 中")
        n = int(m.group(1))
        self.assertEqual(n, 30,
            f"v3.18: AI 辅助文案默认最长时长应为 30s, got {n}s")

    def test_default_range_20_30_produces_correct_chars(self):
        """v3.18: 默认 20-30s 估字应在 85-156 字范围 (zh, 100% 语速)."""
        chars_min, chars_max = estimate_chars_for_duration_range("zh", 20, 30, 0)
        self.assertGreaterEqual(chars_min, 80,
            f"v3.18: 20s zh 估字下限应 >= 80, got {chars_min}")
        self.assertLessEqual(chars_max, 160,
            f"v3.18: 30s zh 估字上限应 <= 160, got {chars_max}")

    def test_no_legacy_15s_default(self):
        """v3.18 回归: 旧版 15s 默认值不应回潮."""
        src = self._on_wizard_ai_script_src()
        # 排除注释/字符串里的 15 (只检查 setValue)
        legacy_matches = re.findall(r"dur_min\.setValue\(\s*15\s*\)", src)
        self.assertEqual(legacy_matches, [],
            f"v3.18 回归: 不应再有 'dur_min.setValue(15)', got {legacy_matches}")


if __name__ == "__main__":
    unittest.main()
