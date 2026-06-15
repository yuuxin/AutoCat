import unittest
from unittest.mock import patch

from autokat.core.writer import (
    build_safe_script, estimate_chars_for_duration_range, generate_publish_title,
    generate_script_by_topic_detailed, script_similarity, translate_text,
    validate_script_quality,
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
        # 用 mid-range 长度 (60~223 chars, 在 0.5*min=59.5 ~ 1.5*max=223.5 之间)
        # 但仍 invalid 的响应来测试 retry 机制。16/14 chars 这种 wildly off 的
        # 响应会触发 _is_wildly_off 早 fail，由 test_wildly_off_first_attempt_calls_model_once 单独覆盖。
        _retry_invalid = (
            "今天给大家推荐几款家居好物，质量好价格实惠，款式新颖百搭耐看，"
            "欢迎选购下单，每一款都经过精心挑选，性价比超高，"
            "值得入手，错过可惜，赶紧加购下单吧。"
        )
        deepseek.side_effect = [
            _retry_invalid,
            _retry_invalid,
            valid,
        ]
        result = generate_script_by_topic_detailed(
            TOPIC, "种草推荐",
            target_chars_min=MIN_CHARS, target_chars_max=MAX_CHARS,
            provider="deepseek",
        )
        # After the ai_providers refactor the DeepSeek backend is now a
        # named provider, so the source field carries the class name.
        self.assertEqual(result["source"], "DeepSeekWriterProvider")
        self.assertEqual(deepseek.call_count, 3)
        self.assertTrue(result["quality"]["valid"])

    @patch("autokat.core.writer.DEEPSEEK_API_KEY", "")
    @patch("autokat.core.writer._call_local_model", return_value=None)
    def test_all_backend_failures_raise_with_manual_input_hint(self, local):
        """v3.2: AI 失败不再用 build_safe_script 兜底, 直接 raise 提示用户手动录入。
        用户报告: 兜底模板「无意义」。"""
        with self.assertRaises(RuntimeError) as ctx:
            generate_script_by_topic_detailed(
                TOPIC, "种草推荐", extra_instruction="第5条",
                target_chars_min=MIN_CHARS, target_chars_max=MAX_CHARS,
            )
        err = str(ctx.exception)
        self.assertEqual(local.call_count, 3,
                          "local 失败 3 次后应该 raise (不再落兜底模板)")
        # 异常必须含: 失败原因 + 手动录入建议 + provider 名
        self.assertIn("手动录入", err, "异常必须提示用户手动录入")
        self.assertIn("LocalWriterProvider", err, "异常必须指明哪个 provider 失败")
        self.assertNotIn("安全模板", err, "v3.2: 异常中不应再出现「安全模板」字样")

    @patch("autokat.core.writer.DEEPSEEK_API_KEY", "configured")
    @patch("autokat.core.writer._call_local_model")
    @patch("autokat.core.writer._call_deepseek_api", return_value=None)
    def test_explicit_deepseek_never_silently_calls_local(self, deepseek, local):
        """v3.2: DeepSeek 失败不落兜底模板, 直接 raise。绝不静默切到 local。"""
        with self.assertRaises(RuntimeError) as ctx:
            generate_script_by_topic_detailed(
                TOPIC, "种草推荐", extra_instruction="第5条",
                target_chars_min=MIN_CHARS, target_chars_max=MAX_CHARS,
                provider="deepseek",
            )
        self.assertEqual(deepseek.call_count, 3)
        local.assert_not_called()  # contract: explicit deepseek 失败不静默切 local
        err = str(ctx.exception)
        self.assertIn("DeepSeekWriterProvider", err)
        self.assertIn("手动录入", err)

    @patch("autokat.core.writer.DEEPSEEK_API_KEY", "configured")
    @patch("autokat.core.writer._call_local_model", return_value=None)
    @patch("autokat.core.writer._call_deepseek_api")
    def test_default_provider_is_local_even_when_deepseek_is_configured(
        self, deepseek, local,
    ):
        """v3.2: local 失败也直接 raise, 不落兜底模板。"""
        with self.assertRaises(RuntimeError) as ctx:
            generate_script_by_topic_detailed(
                TOPIC, "种草推荐",
                target_chars_min=MIN_CHARS, target_chars_max=MAX_CHARS,
            )
        self.assertEqual(local.call_count, 3)
        deepseek.assert_not_called()  # contract: default=local, deepseek 不该被调
        err = str(ctx.exception)
        self.assertIn("LocalWriterProvider", err)
        self.assertIn("手动录入", err)

    @patch("autokat.core.writer.DEEPSEEK_API_KEY", "configured")
    @patch("autokat.core.writer._call_local_model")
    @patch("autokat.core.writer._call_deepseek_api", return_value=None)
    def test_explicit_deepseek_title_never_calls_local(self, deepseek, local):
        """v3.2: 标题 AI 失败也不再「用首句截断」兜底, 直接 raise 提示用户手动录入。"""
        with self.assertRaises(RuntimeError) as ctx:
            generate_publish_title("时尚女鞋让日常搭配更有精神。", provider="deepseek")
        self.assertEqual(deepseek.call_count, 1)
        local.assert_not_called()  # contract: explicit deepseek 失败不切 local
        err = str(ctx.exception)
        self.assertIn("手动录入", err)
        self.assertIn("DeepSeek", err)

    @patch("autokat.core.writer.DEEPSEEK_API_KEY", "configured")
    @patch("autokat.core.writer._call_local_model", return_value="local translation")
    @patch("autokat.core.writer._call_deepseek_api")
    def test_local_translation_never_calls_deepseek(self, deepseek, local):
        self.assertEqual(translate_text("你好", "en", provider="local"), "local translation")
        local.assert_called_once()
        deepseek.assert_not_called()


if __name__ == "__main__":
    unittest.main()
