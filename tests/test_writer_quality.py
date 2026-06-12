import unittest
from unittest.mock import patch

from autokat.core.writer import (
    build_safe_script, estimate_chars_for_duration_range, generate_script_by_topic_detailed,
    script_similarity, validate_script_quality,
)


TOPIC = "时尚女鞋"
MIN_CHARS, MAX_CHARS = estimate_chars_for_duration_range("zh", 25, 30)


def quality(text, **kwargs):
    return validate_script_quality(
        text, TOPIC, lang="zh",
        target_chars_min=MIN_CHARS, target_chars_max=MAX_CHARS,
        **kwargs,
    )


class WriterQualityTests(unittest.TestCase):
    def test_rejects_user_reported_meta_reply(self):
        text = (
            "当然可以，请告诉我您想要的主题是什么？比如新款运动鞋、经典皮鞋"
            "或者优雅女士鞋。这样我就能为您创作出最符合需求的文案了。"
        )
        result = quality(text)
        self.assertFalse(result["valid"])
        self.assertTrue(any("追问" in reason for reason in result["reasons"]))

    def test_rejects_wrong_topic_language_short_and_placeholder(self):
        self.assertFalse(quality("今天介绍厨房收纳。")["valid"])
        self.assertFalse(quality("fashion shoes " * 20)["valid"])
        self.assertFalse(quality("时尚女鞋【卖点】——！")["valid"])

    def test_translated_result_can_validate_without_literal_source_topic(self):
        text = "A confident everyday look starts with the right pair of shoes. " * 4
        result = validate_script_quality(
            text, TOPIC, lang="en", target_chars_min=100,
            require_topic=False,
        )
        self.assertTrue(result["valid"], result["reasons"])

    def test_rejects_unsupported_claims_without_product_details(self):
        text = build_safe_script(TOPIC, 0, MIN_CHARS, MAX_CHARS).replace(
            "日常搭配思路", "环保透气面料",
        )
        self.assertFalse(quality(text)["valid"])
        self.assertTrue(
            quality(text, features="环保透气面料")["valid"],
            "Explicit user-provided claims must be allowed",
        )

    def test_rejects_high_batch_similarity(self):
        text = build_safe_script(TOPIC, 1, MIN_CHARS, MAX_CHARS)
        result = quality(text, accepted_texts=[text])
        self.assertFalse(result["valid"])
        self.assertGreater(result["max_similarity"], 0.70)

    def test_safe_templates_are_valid_and_diverse(self):
        accepted = []
        for index in range(5):
            text = build_safe_script(TOPIC, index, MIN_CHARS, MAX_CHARS)
            result = quality(text, accepted_texts=accepted)
            self.assertTrue(result["valid"], result["reasons"])
            accepted.append(text)
        self.assertLess(script_similarity(accepted[0], accepted[1]), 0.70)

    @patch("autokat.core.writer.DEEPSEEK_API_KEY", "configured")
    @patch("autokat.core.writer._call_local_model", return_value=None)
    @patch("autokat.core.writer._call_deepseek_api")
    def test_retries_until_valid(self, deepseek, _local):
        valid = build_safe_script(TOPIC, 2, MIN_CHARS, MAX_CHARS)
        deepseek.side_effect = [
            "请告诉我您想要的主题是什么？",
            "今天介绍厨房收纳，欢迎关注。",
            valid,
        ]
        result = generate_script_by_topic_detailed(
            TOPIC, "种草推荐",
            target_chars_min=MIN_CHARS, target_chars_max=MAX_CHARS,
        )
        self.assertEqual(result["source"], "DeepSeek")
        self.assertEqual(deepseek.call_count, 3)
        self.assertTrue(result["quality"]["valid"])

    @patch("autokat.core.writer.DEEPSEEK_API_KEY", "")
    @patch("autokat.core.writer._call_local_model", return_value=None)
    def test_all_backend_failures_use_safe_template(self, local):
        result = generate_script_by_topic_detailed(
            TOPIC, "种草推荐", extra_instruction="第5条",
            target_chars_min=MIN_CHARS, target_chars_max=MAX_CHARS,
        )
        self.assertEqual(result["source"], "安全模板")
        self.assertEqual(local.call_count, 3)
        self.assertTrue(result["quality"]["valid"])


if __name__ == "__main__":
    unittest.main()
