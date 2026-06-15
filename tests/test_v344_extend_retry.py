"""v3.4.4 守护测试."""
import unittest
from unittest.mock import patch
from autokat.core.writer import _topic_terms, generate_script_by_topic_detailed


class TopicTermsOneCharTests(unittest.TestCase):
    def test_topic_terms_includes_shoe_one_char(self):
        terms = _topic_terms("时尚女鞋")
        self.assertIn("鞋", terms, "v3.4.2: 必须含单字 '鞋'")

    def test_topic_terms_still_includes_full_topic(self):
        terms = _topic_terms("时尚女鞋")
        self.assertEqual(terms[0], "时尚女鞋", "完整 topic 应该是第一个")

    def test_validate_script_quality_no_topic_misjudgment(self):
        from autokat.core.writer import validate_script_quality
        text = "想为日常穿搭多一点灵感, 其实一双合适的鞋就能带来很大的变化。"
        r = validate_script_quality(text, "时尚女鞋", lang="zh")
        topic_reasons = [x for x in r["reasons"] if "未围绕选题" in x]
        self.assertEqual(topic_reasons, [], f"v3.4.2: '合适的鞋' 不应触发 topic 误伤, reasons={r['reasons']}")
        self.assertEqual(topic_reasons, [], f"v3.4.2: '合适的鞋' 不应触发 topic 误伤, reasons={r['reasons']}")


class ExtendRetryFlowTests(unittest.TestCase):
    def test_short_output_gets_extended_on_retry(self):
        """Qwen 50 字 -> EXTEND 救场 -> 130+ 字"""
        TOPIC = "时尚女鞋"
        attempts = []
        def fake_qwen(prompt, max_length):
            attempts.append("EXTEND" in prompt)
            if "EXTEND" in prompt:
                return ("时尚女鞋通勤逛街约会都能轻松切换, 百搭款式不挑任何风格, 让你省心又自在。"
                        "春夏季节穿上轻便的款式, 整体造型也跟着松弛自然起来, 走起路来都更有节奏感。"
                        "用舒服的步调走出自己的味道, 让每个普通一天都多一点新鲜感。")
            else:
                return "想为日常穿搭多一点灵感, 其实一双合适的鞋就能带来很大的变化。"
        with patch("autokat.core.writer.DEEPSEEK_API_KEY", ""), \
             patch("autokat.core.writer._call_local_model", side_effect=fake_qwen):
            result = generate_script_by_topic_detailed(
                TOPIC, "种草推荐",
                target_chars_min=107, target_chars_max=142, max_attempts=3,
            )
        self.assertTrue(result["quality"]["valid"], f"v3.4.3: 应能通过, reasons={result['quality']['reasons']}")
        self.assertGreaterEqual(len(attempts), 2)
        self.assertFalse(attempts[0])
        self.assertTrue(attempts[1])

    def test_unrelated_output_fails_after_3_retries(self):
        """无关输出 3 次后 raise (v3.2 行为不变)"""
        TOPIC = "时尚女鞋"
        def fake_qwen(prompt, max_length):
            return "今天天气真好, 我们来聊聊关于生活的小技巧吧。"
        with patch("autokat.core.writer.DEEPSEEK_API_KEY", ""), \
             patch("autokat.core.writer._call_local_model", side_effect=fake_qwen):
            with self.assertRaises(RuntimeError) as ctx:
                generate_script_by_topic_detailed(
                    TOPIC, "种草推荐",
                    target_chars_min=107, target_chars_max=142, max_attempts=3,
                )
        err = str(ctx.exception)
        self.assertTrue("字数不足" in err or "未围绕选题" in err, f"实际: {err[:200]}")


if __name__ == "__main__":
    unittest.main()
