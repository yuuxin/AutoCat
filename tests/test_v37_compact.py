"""v3.7 守护测试: prompt 精简 + 反泄漏段不再被拷贝 + validation 软化."""
import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

import autokat.core.writer as writer
from autokat.core.writer import (
    _build_prompt, _format_capability_summary_prompt,
    generate_script_by_topic_detailed,
)


class PromptSizeTests(unittest.TestCase):
    """v3.7: prompt 整体长度从 2669 -> 目标 < 1500 chars"""

    def test_25_30s_prompt_under_2000(self):
        """25-30s 视频 prompt 应在 2000 chars 以内 (v3.6 是 2669)"""
        prompt = _build_prompt(
            "春夏女鞋", "种草推荐", detail=None, features=None,
            lang="zh",
            target_chars_min=107, target_chars_max=156,
            target_duration_min=25, target_duration_max=30,
        ) + _format_capability_summary_prompt("鞋子/特写/通勤/自然光/百搭")
        self.assertLess(len(prompt), 2000,
            f"v3.7: 25-30s prompt 应 < 2000 chars, got {len(prompt)}")

    def test_prompt_contains_actual_duration(self):
        """prompt 必须含 '25-30 秒' 不含硬编码 '30-60 秒'"""
        prompt = _build_prompt(
            "春夏女鞋", "种草推荐", detail=None, features=None,
            lang="zh",
            target_duration_min=25, target_duration_max=30,
        )
        self.assertIn("25-30 秒", prompt)
        self.assertNotIn("30-60 秒", prompt)


class AntiLeakCompactnessTests(unittest.TestCase):
    """v3.7: 反泄漏段 6 行 -> 1 行, 不再被模型拷贝"""

    def test_helper_no_leak_explanatory_text(self):
        """v3.7 helper 不应含反例字符串 (v3.6 '女鞋/初夏/通勤/...' 被模型拷贝)"""
        prompt = _format_capability_summary_prompt("鞋子/特写/通勤/自然光")
        # 不应再出现反例字符串 (v3.6 风险)
        forbidden = ("女鞋/初夏", "标签1/标签2", "严禁作为文案正文")
        for f in forbidden:
            self.assertNotIn(f, prompt,
                f"v3.7: helper 不应再含 '{f}' (被模型拷贝过的反例)")

    def test_no_three_banned_sections(self):
        """v3.7 核心: 3 个'禁止'段已合并为 1 个【禁止】段, 不重复堆叠"""
        prompt = _build_prompt(
            "春夏女鞋", "种草推荐", detail=None, features=None,
            lang="zh",
        ) + _format_capability_summary_prompt("鞋子/特写/通勤/自然光")
        # 旧版有 3 段 【禁止】 / 【视觉信息缺失 · 禁止编造外观】 / 【禁止捏造】
        old_section_markers = ("【视觉信息缺失 · 禁止编造外观】", "【禁止捏造】")
        for marker in old_section_markers:
            self.assertNotIn(marker, prompt,
                f"v3.7: 不应再有 '{marker}' 段, 已合并到【禁止】")
        # 应该有新的【禁止】段
        self.assertIn("【禁止】", prompt, "v3.7: 应该有合并的【禁止】段")


class ValidationSofteningTests(unittest.TestCase):
    """v3.7: 软化 _UNSUPPORTED_PRODUCT_CLAIMS, 透气/百搭等通用属性允许"""

    def test_breathable_allowed(self):
        """v3.7 关键: '透气' 是鞋类通用属性, 不应再 fail"""
        from autokat.core.writer import validate_script_quality
        # '透气' 是常见鞋类属性
        text = ("想为日常穿搭多一点灵感, 其实一双时尚女鞋就能带来很大的变化。"
                "透气舒适让你不再为脚汗烦恼, 春夏季节轻松穿出清爽感。"
                "百搭的款式配什么都自然, 让你省心又自在。")
        r = validate_script_quality(text, "时尚女鞋", lang="zh",
                                     target_chars_min=107, target_chars_max=142)
        reasons = r.get("reasons", [])
        # 不应再有 "包含未提供的具体属性: 透气"
        leaky_reasons = [x for x in reasons if "透气" in x]
        self.assertEqual(leaky_reasons, [],
            f"v3.7: '透气' 应允许 (鞋类通用属性), got reasons: {reasons}")

    def test_comfortable_allowed(self):
        """'百搭' 是常见鞋类属性, 不应 fail"""
        from autokat.core.writer import validate_script_quality
        text = ("想为日常穿搭多一点灵感, 其实一双时尚女鞋就能带来很大的变化。"
                "百搭款式通勤逛街约会轻松切换, 让你每天都有好心情。")
        r = validate_script_quality(text, "时尚女鞋", lang="zh",
                                     target_chars_min=107, target_chars_max=142)
        reasons = r.get("reasons", [])
        leaky_reasons = [x for x in reasons if "百搭" in x]
        self.assertEqual(leaky_reasons, [],
            f"v3.7: '百搭' 应允许, got reasons: {reasons}")


if __name__ == "__main__":
    unittest.main()
