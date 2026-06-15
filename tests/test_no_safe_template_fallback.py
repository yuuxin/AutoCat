"""Regression tests for v3.2: 移除 build_safe_script 兜底。

用户报告 (任务 239 follow-up): 「不要兜底模式, 不要兜底模板, 无意义」
- AI 重试耗尽时, 不再用 build_safe_script 输出固定模板,
  而应该 raise 清晰错误, 提示用户手动录入文案。

这些测试守护:
- generate_script_by_topic_detailed 不再返回 source 为「安全模板」的 dict
- generate_script_by_topic_detailed 在 AI 失败时 raise (而不是落兜底)
- generate_publish_title 不再有「用 narration 第一句前 N 字」兜底
"""
import unittest
from unittest.mock import patch

import autokat.core.writer as writer
from autokat.core.writer import (
    build_safe_script, generate_publish_title, generate_script_by_topic_detailed,
)


class NoSafeTemplateFallbackInScriptGenTests(unittest.TestCase):
    """v3.2: 文案生成 AI 失败时直接 raise, 不再用 build_safe_script 兜底"""

    def test_no_safe_template_source_in_writer(self):
        """writer.py 源码里不应再出现 source='安全模板' 这种兜底标识"""
        import inspect
        src = inspect.getsource(writer)
        self.assertNotIn('"安全模板"', src,
            "v3.2: writer.py 不应再出现 source='安全模板' 这种兜底标识")
        self.assertNotIn("'安全模板'", src,
            "v3.2: writer.py 不应再出现 source='安全模板' 这种兜底标识")

    def test_no_build_safe_script_call_in_generate_detailed(self):
        """generate_script_by_topic_detailed 源码里不应再调 build_safe_script"""
        import inspect
        src = inspect.getsource(generate_script_by_topic_detailed)
        self.assertNotIn("build_safe_script(",
            src,
            "v3.2: generate_script_by_topic_detailed 不应再调 build_safe_script 兜底")

    def test_ai_failure_raises_with_manual_input_hint(self):
        """v3.3: AI 失败必须 raise, 错误信息含具体 provider 失败原因。
        旧版 (v3.2) 错误信息含「手动录入」, v3.3 改为 fail-fast 直接抛 provider
        自己的错误 (如「本地模型未返回有效正文」) 更精准。"""
        with patch("autokat.core.writer.DEEPSEEK_API_KEY", ""), \
             patch("autokat.core.writer._call_local_model", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                generate_script_by_topic_detailed(
                    "时尚女鞋", "种草推荐",
                    target_chars_min=100, target_chars_max=130,
                )
        err = str(ctx.exception)
        # v3.3: 永久错误立即抛, 异常透传 provider 自己的错误
        self.assertIn("LocalWriterProvider", err,
            f"v3.3: 异常必须指明 LocalWriterProvider 失败 (fail-fast 透传), 实际: {err!r}")
        # 不能再含「手动录入」 (那是 v3.2 旧 contract, v3.3 改 fail-fast)
        self.assertNotIn("手动录入", err,
            f"v3.3: 永久错误不应再被「手动录入」包装, 实际: {err!r}")

    def test_wildly_off_raises_with_manual_input_hint(self):
        """首次输出严重偏离字数范围, 早 fail 后也要 raise (不再走兜底)"""
        call_count = [0]

        def fake_generate(self, prompt, max_tokens):
            call_count[0] += 1
            return "abcde"  # 5 chars, 远低于 min

        with patch("autokat.core.ai_providers.build_writer_provider") as mock_factory:
            mock_factory.return_value = type(
                "FakeProvider", (), {"generate": fake_generate}
            )()
            with self.assertRaises(RuntimeError) as ctx:
                generate_script_by_topic_detailed(
                    "时尚女鞋",
                    target_chars_min=119, target_chars_max=142,
                    max_attempts=3,
                )
        self.assertEqual(call_count[0], 1, "_is_wildly_off 早 fail 应只调 1 次")
        self.assertIn("手动录入", str(ctx.exception))

    def test_build_safe_script_function_kept_but_unused(self):
        """build_safe_script 函数可以保留 (供未来手动场景), 但不应再被自动调用"""
        import inspect
        sig = inspect.signature(build_safe_script)
        self.assertIn("topic", sig.parameters)
        result = build_safe_script("测试", 0)
        self.assertTrue(len(result) > 0)


class NoFirstSentenceFallbackInPublishTitleTests(unittest.TestCase):
    """v3.2: 标题 AI 失败时不再用 narration 第一句截断兜底"""

    def test_publish_title_no_first_sentence_fallback(self):
        """AI 全部失败时 generate_publish_title 必须 raise, 不再返回截断首句"""
        with patch("autokat.core.writer.DEEPSEEK_API_KEY", "configured"), \
             patch("autokat.core.writer._call_local_model", return_value=None), \
             patch("autokat.core.writer._call_deepseek_api", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                generate_publish_title("时尚的春夏女鞋让你出门更有精神。", provider="deepseek")
        err = str(ctx.exception)
        self.assertIn("手动录入", err)
        self.assertIn("标题", err)


if __name__ == "__main__":
    unittest.main()
