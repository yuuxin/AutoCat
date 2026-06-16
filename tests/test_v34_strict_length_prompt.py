"""v3.4 严格字数 prompt + 自检标记剥除守护测试."""
import re
import unittest
import autokat.core.writer as writer
from autokat.core.writer import _build_prompt, _clean_result


class StrictLengthPromptTests(unittest.TestCase):
    def _get_prompt(self, topic="时尚女鞋", style="种草推荐", mn=107, mx=142):
        return _build_prompt(topic, style, detail=None, features=None, lang="zh", extra_instruction=None, target_chars_min=mn, target_chars_max=mx)

    def test_prompt_contains_exact_target_not_just_range(self):
        prompt = self._get_prompt()
        self.assertIn("124", prompt, "v3.4: prompt 必须含精确目标字数 124")

    def test_prompt_specifies_3_sentence_structure_v37(self):
        """v3.7: few-shot 4 句 -> 3 句 (适配 25-30s 短视频)"""
        prompt = self._get_prompt()
        # v3.7 改为 3 句结构, 不再硬编码 "4 个短句"
        self.assertIn("3 句共", prompt, "v3.7: prompt 必须明确要求 3 句结构")

    def test_prompt_has_self_check_marker_instruction(self):
        prompt = self._get_prompt()
        self.assertIn("[字数:XXX]", prompt, "v3.4: prompt 必须含 [字数:XXX] 自检行")

    def test_prompt_has_few_shot_example(self):
        """v3.7: 3 句 few-shot (适配 25-30s)"""
        prompt = self._get_prompt()
        self.assertIn("参考结构", prompt, "v3.4: prompt 必须含参考结构小节")
        quoted = re.findall(r'"[^"]{10,60}"', prompt)
        # v3.7 改 3 句
        self.assertGreaterEqual(len(quoted), 3, f"v3.7: prompt 必须含 3+ 句带引号的示例, 实际 {len(quoted)}")

    def test_prompt_warns_about_rejection_range(self):
        prompt = self._get_prompt()
        self.assertIn("107", prompt)
        self.assertIn("142", prompt)


class SelfCheckMarkerStrippingTests(unittest.TestCase):
    def test_strip_bracketed_marker(self):
        text = "春夏穿搭的灵感其实很简单。百搭款式配什么都自然。[字数:130]"
        out = _clean_result(text)
        self.assertNotIn("[字数:130]", out)
        self.assertNotIn("130", out, f"v3.4: [字数:130] 必须被剥, 实际: {out!r}")

    def test_strip_marker_with_space(self):
        text = "春夏穿搭的灵感其实很简单。[字数: 130]"
        out = _clean_result(text)
        self.assertNotIn("130", out, f"v3.4: [字数: 130] 必须被剥, 实际: {out!r}")

    def test_strip_marker_with_fullwidth_colon(self):
        text = "春夏穿搭的灵感其实很简单。[字数：130]"
        out = _clean_result(text)
        self.assertNotIn("130", out, f"v3.4: [字数：130] 必须被剥, 实际: {out!r}")

    def test_strip_marker_with_parentheses(self):
        text = "春夏穿搭的灵感其实很简单。(字数:130)"
        out = _clean_result(text)
        self.assertNotIn("130", out, f"v3.4: (字数:130) 必须被剥, 实际: {out!r}")

    def test_strip_marker_with_fullwidth_parentheses(self):
        text = "春夏穿搭的灵感其实很简单。（字数：130）"
        out = _clean_result(text)
        self.assertNotIn("130", out, f"v3.4: （字数：130） 必须被剥, 实际: {out!r}")

    def test_strip_standalone_marker_line(self):
        text = "春夏穿搭的灵感其实很简单。\n\n字数: 130"
        out = _clean_result(text)
        self.assertNotIn("130", out, f"v3.4: 独立行 '字数: 130' 必须被剥, 实际: {out!r}")

    def test_strip_marker_with_length_keyword(self):
        text = "春夏穿搭的灵感其实很简单。[长度:130]"
        out = _clean_result(text)
        self.assertNotIn("130", out, f"v3.4: [长度:130] 必须被剥, 实际: {out!r}")


class EndToEndUsableOutputTests(unittest.TestCase):
    def test_model_output_with_marker_becomes_clean_script(self):
        model_output = (
            "想为日常穿搭多一点灵感, 其实时尚女鞋就能带来很大的变化。"
            "春夏季节穿上轻便的款式, 整体造型也会跟着松弛自然起来。"
            "百搭的设计不挑任何风格, 通勤逛街约会都能轻松切换。"
            "用舒服的步调走出自己的味道, 让每个普通一天都多一点新鲜感。"
            "[字数:118]"
        )
        out = _clean_result(model_output)
        self.assertNotIn("[字数", out)
        self.assertNotIn("118", out, f"v3.4: 标记应被剥, 实际: {out!r}")
        self.assertIn("穿搭", out)
        self.assertIn("百搭", out)
        self.assertGreater(len(out), 100, f"v3.4: 剥标记后约 118 chars, 实际 {len(out)}")
        self.assertLess(len(out), 130, f"v3.4: 剥标记后约 118 chars, 实际 {len(out)}")

    def test_validation_accepts_cleaned_in_range_output(self):
        from autokat.core.writer import validate_script_quality
        model_output = (
            "想为日常穿搭多一点灵感, 其实时尚女鞋就能带来很大的变化。"
            "春夏季节穿上轻便的款式, 整体造型也会跟着松弛自然起来。"
            "百搭的设计不挑任何风格, 通勤逛街约会都能轻松切换。"
            "用舒服的步调走出自己的味道, 让每个普通一天都多一点新鲜感。"
            "[字数:118]"
        )
        cleaned = _clean_result(model_output)
        result = validate_script_quality(cleaned, "时尚女鞋", lang="zh", target_chars_min=107, target_chars_max=142)
        self.assertTrue(result["valid"], f"v3.4: 剥标记后 4 句脚本应在 107-142 范围, reasons={result['reasons']}")


if __name__ == "__main__":
    unittest.main()
