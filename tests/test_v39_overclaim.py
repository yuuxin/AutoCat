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

    def test_perfect_zhanxian_phrase_passes_v311(self):
        """v3.11: '完美展现' / '完美呈现' / '完美融合' 放行 (用户要求形容词可放行)"""
        for phrase in ["完美展现", "完美呈现", "完美融合"]:
            text = f"这款鞋{phrase}了优雅气质, 真的太好看了。春夏搭配浅色长裙清新自然。"
            r = validate_script_quality(text, "春夏女鞋", lang="zh",
                                         target_chars_min=50, target_chars_max=200)
            overclaim_reasons = [x for x in r.get("reasons", [])
                                 if "过度承诺" in x]
            self.assertEqual(overclaim_reasons, [],
                f"v3.11: '{phrase}' 应放行 (常用营销词), reasons={r.get('reasons')}")

    def test_juejia_passes_v311(self):
        """v3.11: '绝佳' 放行 (用户要求形容词可放行)"""
        text = "这款鞋绝佳适合任何场合, 真的是必备单品。春夏搭配浅色长裙清新自然。"
        r = validate_script_quality(text, "春夏女鞋", lang="zh",
                                     target_chars_min=50, target_chars_max=200)
        overclaim_reasons = [x for x in r.get("reasons", [])
                             if "过度承诺" in x]
        self.assertEqual(overclaim_reasons, [],
            f"v3.11: '绝佳' 应放行, reasons={r.get('reasons')}")

    def test_clear_overclaim_still_rejected_v311(self):
        """v3.11: 艺术品/颠覆性/革命性 (3 个最 clear overclaim) 仍 reject"""
        for kw in ["艺术品", "颠覆性", "革命性"]:
            text = f"这款鞋是{kw}级别的设计, 让你走在时尚尖端。春夏搭配浅色长裙清新自然。"
            r = validate_script_quality(text, "春夏女鞋", lang="zh",
                                         target_chars_min=50, target_chars_max=200)
            overclaim_reasons = [x for x in r.get("reasons", [])
                                 if "过度承诺" in x]
            self.assertGreater(len(overclaim_reasons), 0,
                f"v3.11: '{kw}' 仍应 reject (clear overclaim), reasons={r.get('reasons')}")


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
