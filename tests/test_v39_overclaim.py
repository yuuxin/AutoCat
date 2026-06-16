"""v3.9 修 v3.8 回归: validation 与 prompt 一致性 + '完美' 误杀."""
import unittest

import autokat.core.writer as writer
from autokat.core.writer import _OVERCLAIMS_NO_SUPPORT, validate_script_quality, _build_prompt


class PerfectOverclaimTests(unittest.TestCase):
    """v3.9 修 1: '完美' 是常用形容词, 不应被 reject"""

    def test_perfect_alone_passes(self):
        """'完美适合你的风格' 应当 pass (v3.9 删 '完美')
        避开 完美展现/完美呈现/完美融合 (这 3 个短语仍 reject)"""
        for text in [
            "想要为日常穿搭带来更多灵感, 其实完美适合你的风格。",
            "春夏女鞋做到舒适与时尚, 让你穿出自信的完美状态。",
            "这款鞋的版型真的完美, 强烈推荐给每个女生。",
        ]:
            r = validate_script_quality(text, "春夏女鞋", lang="zh",
                                         target_chars_min=50, target_chars_max=200)
            overclaim_reasons = [x for x in r.get("reasons", [])
                                 if "过度承诺" in x or "overclaim" in x.lower()]
            self.assertEqual(overclaim_reasons, [],
                f"v3.9: '完美' (常用词) 不应触发过度承诺 fail, text={text!r}, reasons={r.get('reasons')}")

    def test_perfect_preserves_overclaim_phrases(self):
        """'完美展现' / '完美呈现' / '完美融合' 仍应被 reject (3 个 overclaim 短语保留)"""
        for phrase in ["完美展现", "完美呈现", "完美融合"]:
            text = f"这款鞋{phrase}了优雅气质, 真的太好看了。"
            r = validate_script_quality(text, "春夏女鞋", lang="zh",
                                         target_chars_min=50, target_chars_max=200)
            overclaim_reasons = [x for x in r.get("reasons", [])
                                 if "过度承诺" in x or "overclaim" in x.lower()]
            self.assertGreater(len(overclaim_reasons), 0,
                f"v3.9: '{phrase}' 仍应触发过度承诺 fail, reasons={r.get('reasons')}")

    def test_juejia_still_rejected(self):
        """'绝佳' 是明确 overclaim, 仍应 reject"""
        text = "这款鞋绝佳适合任何场合, 真的是必备单品。"
        r = validate_script_quality(text, "春夏女鞋", lang="zh",
                                     target_chars_min=50, target_chars_max=200)
        overclaim_reasons = [x for x in r.get("reasons", [])
                             if "过度承诺" in x or "overclaim" in x.lower()]
        self.assertGreater(len(overclaim_reasons), 0,
            f"v3.9: '绝佳' 仍应 reject, reasons={r.get('reasons')}")


class PromptValidationConsistencyTests(unittest.TestCase):
    """v3.9 修 2: prompt 加上常见 overclaim 短语, 防止模型误用"""

    def test_prompt_lists_common_overclaim_phrases(self):
        """prompt 应含 完美展现/完美呈现/完美融合/绝佳 (v3.8 漏了)"""
        prompt = _build_prompt(
            "春夏女鞋", "种草推荐", detail=None, features=None,
            lang="zh",
            target_chars_min=107, target_chars_max=156,
        )
        for phrase in ["完美展现", "完美呈现", "完美融合", "绝佳"]:
            self.assertIn(phrase, prompt,
                f"v3.9: prompt 应含 '{phrase}' 提示模型避开")

    def test_validation_list_matches_prompt(self):
        """validation 黑名单 应 <= prompt 提示 + 常用词 (1 个差也算)"""
        prompt = _build_prompt(
            "春夏女鞋", "种草推荐", detail=None, features=None,
            lang="zh",
            target_chars_min=107, target_chars_max=156,
        )
        # v3.8 漏报的字 (v3.9 加回了) - 这些必须在 prompt 里能找到
        must_be_in_prompt = ["完美展现", "完美呈现", "完美融合", "绝佳"]
        missing = [p for p in must_be_in_prompt if p not in prompt]
        self.assertEqual(missing, [],
            f"v3.9: validation 拒绝的 overclaim 短语必须在 prompt 提示, missing={missing}")


if __name__ == "__main__":
    unittest.main()
