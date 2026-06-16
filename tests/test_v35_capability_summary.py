"""v3.5 方案 A 守护测试: capability_summary 提示语改造 + 后台 debug 打印."""
import io
import sys
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

import autokat.core.writer as writer
from autokat.core.writer import (
    _build_prompt, _format_capability_summary_prompt,
    generate_script_by_topic_detailed,
)


class CapabilitySummaryPromptTests(unittest.TestCase):
    """v3.5: 切片能力摘要提示语改造, 不再堵死 AI"""

    def _get_prompt_with_capability(self, capability="鞋子/特写/展示/通勤/自然光"):
        # 走与 generate_script_by_topic_detailed 完全相同的拼接逻辑,
        # 避免测试里的手写 prompt 与运行时 prompt 漂移 (v3.5 重构点)。
        return _build_prompt(
            "时尚女鞋", "种草推荐", detail=None, features=None,
            lang="zh", extra_instruction=None,
            target_chars_min=107, target_chars_max=142,
        ) + _format_capability_summary_prompt(capability)

    def test_old_negative_wording_removed(self):
        """v3.5: 旧版 '不得编造素材无法支持的画面' 已删除"""
        prompt = self._get_prompt_with_capability()
        self.assertNotIn("不得编造素材无法支持的画面", prompt,
                          "v3.5: 旧负面措辞应删除, 不再堵死 AI")

    def test_new_positive_wording_present(self):
        """v3.5: 新版 '可以用这些能力描述具体场景' 引导 AI 真的用 summary"""
        prompt = self._get_prompt_with_capability()
        self.assertIn("可以用这些能力描述具体场景", prompt,
                      "v3.5: 必须有 '可以用这些能力' 的正向引导")

    def test_new_suggests_concrete_examples(self):
        """v3.5: 新版给了 3 个具体例子 (特写展示/通勤/户外自然光) 让小模型照搬"""
        prompt = self._get_prompt_with_capability()
        for example in ("特写展示", "通勤穿搭", "户外自然光"):
            self.assertIn(example, prompt, f"v3.5: 提示词必须含示例 '{example}'")

    def test_forbidden_keeps_specific_attributes_only(self):
        """v3.5: 仍禁止 detail/features 未提供的具体属性 (材质/品牌/价格)"""
        prompt = self._get_prompt_with_capability()
        self.assertIn("材质", prompt)
        self.assertIn("品牌", prompt)
        self.assertIn("价格", prompt)

    def test_summary_field_not_flagged_as_forbidden(self):
        """v3.5 核心: capability_summary 里的 "鞋子" "特写" "通勤" 不会被跨品类误伤"""
        # 直接构造一个含这些 summary 词的 AI 输出, 应通过
        from autokat.core.writer import validate_script_quality
        text = ("想为日常穿搭多一点灵感, 其实一双时尚女鞋就能带来很大的变化。"
                "通勤穿搭一双合适的鞋, 通勤逛街约会都能轻松切换。"
                "百搭的款式配什么都自然, 让你省心又自在。")
        r = validate_script_quality(text, "时尚女鞋", lang="zh",
                                     target_chars_min=107, target_chars_max=142)
        cross_reasons = [x for x in r["reasons"] if "跨品类" in x]
        self.assertEqual(cross_reasons, [],
                          f"v3.5: 用了 summary 里的 '通勤' '合适的鞋' 不应被跨品类误伤, "
                          f"reasons={r['reasons']}")


class DebugPromptPrintTests(unittest.TestCase):
    """v3.5: AI 文案生成时 stderr 后台打印完整 prompt, 便于调试"""

    def test_prompt_printed_to_stderr(self):
        """AI 文案生成时, 完整 prompt 写到 stderr 标 [writer.debug]"""
        TOPIC = "时尚女鞋"
        captured_stderr = io.StringIO()
        with patch("autokat.core.writer.DEEPSEEK_API_KEY", ""), \
             patch("autokat.core.writer._call_local_model",
                   return_value="想为日常穿搭多一点灵感, 其实一双时尚女鞋就能带来很大的变化。"
                                "通勤穿搭一双合适的鞋, 通勤逛街约会都能轻松切换。"
                                "百搭的款式配什么都自然, 让你省心又自在。"), \
             redirect_stderr(captured_stderr):
            try:
                generate_script_by_topic_detailed(
                    TOPIC, "种草推荐",
                    target_chars_min=107, target_chars_max=142,
                    max_attempts=1,
                    material_capabilities="鞋子/特写/通勤/自然光",
                )
            except Exception:
                pass

        err_output = captured_stderr.getvalue()
        self.assertIn("[writer.debug]", err_output,
                      "v3.5: stderr 必须含 [writer.debug] 标头")
        self.assertIn("===== AI PROMPT", err_output,
                      "v3.5: stderr 必须含 '===== AI PROMPT' 标头")
        self.assertIn("===== END PROMPT =====", err_output,
                      "v3.5: stderr 必须含 '===== END PROMPT =====' 收尾标头")
        self.assertIn(TOPIC, err_output,
                      "v3.5: stderr 必须含 topic 上下文 (方便 grep)")
        self.assertIn("【已选素材能力摘要】", err_output,
                      "v3.5: 切片能力摘要段必须在 stderr 里")

    def test_prompt_contains_capability_summary_when_provided(self):
        """如果调用方传了 material_capabilities, 必须出现在 prompt 打印里"""
        TOPIC = "时尚女鞋"
        custom_cap = "鞋子/特写/通勤场景/自然光/穿搭推荐"
        captured_stderr = io.StringIO()
        with patch("autokat.core.writer.DEEPSEEK_API_KEY", ""), \
             patch("autokat.core.writer._call_local_model",
                   return_value="想为日常穿搭多一点灵感, 其实一双时尚女鞋就能带来很大的变化。"
                                "通勤穿搭一双合适的鞋, 通勤逛街约会都能轻松切换。"
                                "百搭的款式配什么都自然, 让你省心又自在。"), \
             redirect_stderr(captured_stderr):
            try:
                generate_script_by_topic_detailed(
                    TOPIC, "种草推荐",
                    target_chars_min=107, target_chars_max=142,
                    max_attempts=1,
                    material_capabilities=custom_cap,
                )
            except Exception:
                pass
        err_output = captured_stderr.getvalue()
        self.assertIn(custom_cap, err_output,
                      "v3.5: 调用方传的 material_capabilities 必须出现在 stderr")


if __name__ == "__main__":
    unittest.main()
