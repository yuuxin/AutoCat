"""Regression tests for the 4 issues reported with task 175.

Issue 1: AI 文案 2 中包含 "节女鞋 #经典单品 #品质保证"
   → 期望: _clean_result 剥除 hashtag，修复孤字

Issue 2: 没选差异化扰动但成片有色彩变化
   → 期望: render_simple 不再硬编码 -colorspace bt709

Issue 3: 字数不匹配 12 次失败
   → 期望: _enforce_char_limit 真用在模型输出上；
          _is_wildly_off 早 fail；
          全部失败走 build_safe_script 兜底

Issue 4: 生成视频 03 时报 RuntimeError 缺帧
   → 期望: dynamic shortfall 改为 warn + log，不 raise
"""
import re
import unittest
from unittest.mock import patch

from autokat.core import writer
from autokat.core.writer import (
    _clean_result, _content_char_count, _enforce_char_limit, _is_wildly_off,
)


# ── Issue 1: hashtag 标签 + 孤字修复 ──────────────────────────

class CleanResultHashtagTests(unittest.TestCase):
    def test_strips_inline_hashtags(self):
        text = "今天聊聊时尚女鞋 #经典单品 #品质保证 值得入手"
        out = _clean_result(text)
        self.assertNotIn("#", out)
        self.assertNotIn("经典单品", out)
        self.assertNotIn("品质保证", out)
        self.assertIn("时尚女鞋", out)

    def test_strips_line_starting_hashtags(self):
        text = "#经典单品\n#品质保证\n这是正文"
        out = _clean_result(text)
        self.assertNotIn("#", out)

    def test_repairs_lone_character_between_punctuation(self):
        # "今天聊一下 ，节 。 时尚女鞋" 中 "节" 前后都是标点/空白，疑似 tokenizer 截断
        text = "今天聊一下 ，节 。 时尚女鞋的搭配"
        out = _clean_result(text)
        words = re.findall(r'[\u4e00-\u9fff]+', out)
        # 孤字 "节" 不应单独存在
        self.assertNotIn('节', words, f"孤字 '节' 不应单独存在，out={out!r}")


# ── Issue 3: 字数硬约束 + 早 fail + 兜底 ───────────────────────

class CharLimitEnforcementTests(unittest.TestCase):
    def test_enforce_truncates_overlong_at_punctuation(self):
        text = ("第一句。" + "中间内容，" * 50 + "结尾。")
        out = _enforce_char_limit(text, max_chars=120, min_chars=80)
        self.assertLessEqual(len(out), 120)
        # 截断后应该以标点结尾
        self.assertTrue(out.endswith(("。", "！", "?", "，", "\n")))

    def test_enforce_warns_on_underlength_without_failing(self):
        text = "太短了"
        out = _enforce_char_limit(text, max_chars=200, min_chars=100)
        self.assertEqual(out, "太短了")

    def test_enforce_no_op_when_in_range(self):
        text = "刚好在范围内的文案内容，不做任何修改"
        out = _enforce_char_limit(text, max_chars=200, min_chars=10)
        self.assertEqual(out, text)


class WildlyOffHelperTests(unittest.TestCase):
    def test_over_1_5x_max_is_wildly_off(self):
        self.assertTrue(_is_wildly_off("a" * 200, 50, 100))

    def test_under_0_5x_min_is_wildly_off(self):
        self.assertTrue(_is_wildly_off("a" * 20, 100, 200))

    def test_in_range_is_not_wildly_off(self):
        self.assertFalse(_is_wildly_off("a" * 100, 50, 200))

    def test_empty_is_wildly_off(self):
        self.assertTrue(_is_wildly_off("", 50, 200))


class RetryShortCircuitTests(unittest.TestCase):
    """端到端：mock 本地模型第一次返回短到离谱的输出，断言只调了 1 次就放弃。"""

    def test_wildly_off_first_attempt_calls_model_once(self):
        call_count = [0]
        # 短到爆 (5 chars, 远低于 0.5 * 119 = 59.5)
        bad_output = "abcde"
        # 备用 fallback（在重试循环结束后走到这里）

        def fake_generate(self, prompt, max_tokens):
            call_count[0] += 1
            return bad_output

        # 注入到 provider 工厂
        with patch("autokat.core.ai_providers.build_writer_provider") as mock_factory:
            mock_factory.return_value = type(
                "FakeProvider", (), {"generate": fake_generate}
            )()
            result = writer.generate_script_by_topic_detailed(
                topic="时尚女鞋",
                target_chars_min=119, target_chars_max=142,
                max_attempts=3,
            )

        # 因为 _is_wildly_off 触发早 fail，模型只该被调 1 次
        self.assertEqual(call_count[0], 1,
                         f"wildly off 应当早 fail，模型只调 1 次，实际 {call_count[0]} 次")
        # 最终结果是 fallback safe template（包含 topic 词）
        self.assertIn("时尚女鞋", result["text"])

    def test_in_range_output_succeeds_on_first_try(self):
        """正常长度的输出应该 1 次过，不需要 fallback。"""
        call_count = [0]
        # 用包含 topic 词且字数合规的中文 mock，长度刚好在 119-142 中间
        _filler = "时尚女鞋搭配推荐，舒适透气百搭好看又耐穿，柔软不磨脚！"
        in_range_output = (_filler * 5)[:130]
        assert 119 <= len(in_range_output) <= 142, f"len={len(in_range_output)}"

        def fake_generate(self, prompt, max_tokens):
            call_count[0] += 1
            return in_range_output

        with patch("autokat.core.ai_providers.build_writer_provider") as mock_factory:
            mock_factory.return_value = type(
                "FakeProvider", (), {"generate": fake_generate}
            )()
            result = writer.generate_script_by_topic_detailed(
                topic="时尚女鞋",
                target_chars_min=119, target_chars_max=142,
                max_attempts=3,
            )

        self.assertEqual(call_count[0], 1)
        self.assertTrue(result["quality"]["valid"])


if __name__ == "__main__":
    unittest.main()
