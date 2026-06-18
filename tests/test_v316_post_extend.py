"""v3.16 守护测试: 字数根因彻底修复 — 「模型/系统字数规则不一致」.

用户报告 (任务 568 follow-up):
  模型认为写了 75 字 (含 #经典单品 等 hashtag) → _clean_result 剥 hashtag
  后只剩 67 字 → 触发「字数不足: 67 < 107」拒收, 3 次 retry 都救不回来。

根因 (autokat/core/writer.py 第 763 行 #\\S+ 剥 hashtag):
  模型按「全部字符」计数 (含 hashtag/emoji/markdown/方括号/空格/换行)
  系统按「清洗后字符」计数 (去掉上面所有)
  差距通常 4-15 字; 当模型输出接近下限时, 清洗后必然掉到下限之下。

v3.16 双层防御:
  1. [Prompt 层] 在 _build_prompt 里写清「字数计算规则」+ 对照示例
  2. [后处理层] 主循环重试耗尽后, 用 _post_extend_if_short 调用模型
     做「聚焦扩写」(与主 prompt 解耦), 用更长版本替换原版
"""
import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from autokat.core.writer import (
    _build_extend_prompt,
    _build_prompt,
    _clean_result,
    _content_char_count,
    _enforce_char_limit,
    _post_extend_if_short,
    _post_compress_if_long,
    generate_script_by_topic_detailed,
)


# ── Prompt 层 (预防) ─────────────────────────────────────────────


class PromptExplicitCountingRulesTests(unittest.TestCase):
    """v3.16 Prompt 层: 显式字数计算规则 + 对照示例."""

    def _p(self) -> str:
        return _build_prompt(
            "时尚女鞋", "种草推荐", lang="zh",
            target_chars_min=107, target_chars_max=142,
        )

    def test_prompt_has_counting_rules_section(self):
        p = self._p()
        self.assertIn("字数计算规则", p,
            "v3.16: prompt 必须含「字数计算规则」章节")

    def test_prompt_explains_excluded_chars(self):
        p = self._p()
        for excluded in ["hashtag", "emoji", "markdown", "空格", "换行"]:
            self.assertIn(excluded, p,
                f"v3.16: prompt 必须显式列出不计的字符类型 — 缺 {excluded!r}")

    def test_prompt_has_counting_example(self):
        p = self._p()
        self.assertIn("对照示例", p,
            "v3.16: prompt 必须含「对照示例」, 让模型理解字数计算")
        self.assertIn("字**", p,
            "v3.16: 对照示例必须给出具体字数值 (**31 字**)")

    def test_prompt_warns_hashtag_doesnt_count(self):
        p = self._p()
        self.assertIn("常见误区", p,
            "v3.16: prompt 必须含「常见误区」, 警告 hashtag 凑字数无效")
        self.assertIn("#经典单品", p,
            "v3.16: 必须用具体 hashtag 例子 #经典单品 警告模型")

    def test_v313_strict_hints_preserved(self):
        p = self._p()
        self.assertIn("1-2 句后就结束", p,
            "v3.16: 必须保留 v3.13 「不要在 1-2 句后就结束」强提示")
        self.assertIn("80 字", p,
            "v3.16: 必须保留 v3.13 「短于 80 字 = 不合格」")
        self.assertIn("4 句", p,
            "v3.16: 必须保留 v3.13 「至少 4 句」要求")


# ── Post-extend 函数 (兜底) ──────────────────────────────────────


class BuildExtendPromptTests(unittest.TestCase):
    """v3.16: _build_extend_prompt 聚焦扩写 prompt."""

    def test_extend_prompt_is_focused_no_full_rebuild(self):
        p = _build_extend_prompt(
            text="想为日常穿搭多一点灵感, 其实一双合适的鞋就能带来很大的变化。",
            gap=80, target_min=107, target_max=142,
            topic="时尚女鞋",
        )
        self.assertIn("只在末尾添加 1-3 句", p,
            "v3.16: extend prompt 必须明确「只在末尾添加」, 禁止重写")
        self.assertIn("不要修改或重写", p,
            "v3.16: extend prompt 必须明确禁止修改原文")

    def test_extend_prompt_includes_counting_rules(self):
        p = _build_extend_prompt(
            text="短文案", gap=80, target_min=100, target_max=150,
            topic="时尚女鞋",
        )
        for excluded in ["hashtag", "emoji", "markdown", "空格"]:
            self.assertIn(excluded, p,
                f"v3.16: extend prompt 也必须含字数规则 — 缺 {excluded!r}")

    def test_extend_prompt_includes_original_text(self):
        p = _build_extend_prompt(
            text="原始文案内容, 让模型看到要扩写什么。",
            gap=50, target_min=80, target_max=120,
            topic="时尚女鞋",
        )
        self.assertIn("原始文案内容, 让模型看到要扩写什么。", p,
            "v3.16: extend prompt 必须含原始文案")

    def test_extend_prompt_includes_gap_and_target(self):
        p = _build_extend_prompt(
            text="x", gap=80, target_min=107, target_max=142, topic="时尚女鞋",
        )
        self.assertIn("80", p, "v3.16: extend prompt 必须含 gap 数值")
        self.assertIn("107", p, "v3.16: extend prompt 必须含 target_min")
        self.assertIn("142", p, "v3.16: extend prompt 必须含 target_max")


class PostExtendIfShortTests(unittest.TestCase):
    """v3.16: _post_extend_if_short 后处理自动扩写函数."""

    def _make_provider(self, side_effects):
        call_count = [0]
        # 用 *args/**kwargs 接住, 避免 mock 签名跟生产调用不一致
        # (生产代码 provider_obj.generate(prompt, max_tokens=N) 通过实例访问会自带 self)
        def generate(*args, **kwargs):
            idx = call_count[0]
            call_count[0] += 1
            if idx >= len(side_effects):
                return side_effects[-1]
            return side_effects[idx]
        provider = type("FakeProvider", (), {"generate": generate})()
        return provider, call_count

    def test_already_in_range_no_extend(self):
        text = "这个文案已经 110 字, 完全在 107-156 范围内, 不用扩写, 就这样。" * 3
        provider, call_count = self._make_provider(["should not be called"])
        result, count, attempts = _post_extend_if_short(
            text=text,
            target_min=100, target_max=200,
            topic="时尚女鞋", provider_obj=provider,
        )
        self.assertEqual(result, text, "v3.16: 已在范围内应返回原文本")
        self.assertEqual(attempts, 0, "v3.16: 已在范围内不应 extend")
        self.assertEqual(call_count[0], 0, "v3.16: 已在范围内不应调用 model")

    def test_short_text_gets_extended(self):
        short = "想为日常穿搭多一点灵感, 其实一双合适的鞋就能带来很大的变化。"
        long_enough = short + (
            "春夏季节穿上轻便的款式, 整体造型也跟着松弛自然起来, "
            "走起路来都更有节奏感。百搭的设计不挑任何风格, "
            "通勤逛街约会出游都能轻松切换。用舒服的步调走出自己的味道。"
        )
        provider, call_count = self._make_provider([long_enough])
        result, count, attempts = _post_extend_if_short(
            text=short, target_min=107, target_max=156,
            topic="时尚女鞋", provider_obj=provider,
        )
        self.assertGreaterEqual(count, 107,
            f"v3.16: extend 后应 >= 107 字, got {count}")
        self.assertGreater(len(result), len(short),
            "v3.16: extend 后应比原文本更长")
        self.assertEqual(attempts, 1, "v3.16: 1 次 extend 就应达标")
        self.assertEqual(call_count[0], 1, "v3.16: 只调用 1 次 model")

    def test_extend_with_shorter_output_rejected(self):
        original = "想为日常穿搭多一点灵感, 其实一双合适的鞋就能带来很大的变化。" * 3
        worse_output = "太短了"
        provider, call_count = self._make_provider([worse_output])
        result, count, attempts = _post_extend_if_short(
            text=original, target_min=107, target_max=156,
            topic="时尚女鞋", provider_obj=provider,
        )
        self.assertEqual(result, original,
            "v3.16: extend 后变短应保留原文本")
        self.assertEqual(attempts, 1, "v3.16: 1 次 break 后停止")

    def test_extend_with_empty_cleaned_rejected(self):
        original = "想为日常穿搭多一点灵感, 其实一双合适的鞋就能带来很大的变化。" * 3
        provider, call_count = self._make_provider(["以下是文案:\n#经典单品 #品质保证"])
        result, count, attempts = _post_extend_if_short(
            text=original, target_min=107, target_max=156,
            topic="时尚女鞋", provider_obj=provider,
        )
        self.assertEqual(result, original,
            "v3.16: 清洗后为空应保留原文本")
        self.assertEqual(attempts, 1, "v3.16: 清洗后为空停止")

    def test_extend_with_hashtag_in_extend_output(self):
        original = "想为日常穿搭多一点灵感, 其实一双合适的鞋就能带来很大的变化。" * 3
        has_hashtag = original + " #经典单品 #品质保证"
        provider, call_count = self._make_provider([has_hashtag])
        result, count, attempts = _post_extend_if_short(
            text=original, target_min=107, target_max=156,
            topic="时尚女鞋", provider_obj=provider,
        )
        cleaned = _clean_result(has_hashtag, topic="时尚女鞋")
        cleaned_count = _content_char_count(cleaned)
        original_count = _content_char_count(original)
        if cleaned_count <= original_count:
            self.assertEqual(result, original,
                "v3.16: 清洗后不增应保留原文本")

    def test_extend_max_attempts_respected(self):
        original = "想为日常穿搭多一点灵感, 其实一双合适的鞋就能带来很大的变化。" * 3
        provider, call_count = self._make_provider(["太短"] * 5)
        result, count, attempts = _post_extend_if_short(
            text=original, target_min=107, target_max=156,
            topic="时尚女鞋", provider_obj=provider,
            max_extend_attempts=2,
        )
        self.assertLessEqual(call_count[0], 2,
            f"v3.16: max_extend_attempts=2 时 model 调用应 <= 2 次, got {call_count[0]}")
        self.assertEqual(result, original,
            "v3.16: 所有 extend 都失败应保留原文本")

    def test_extend_exception_does_not_propagate(self):
        original = "想为日常穿搭多一点灵感, 其实一双合适的鞋就能带来很大的变化。" * 3
        def raise_exc(*args, **kwargs):
            raise RuntimeError("模拟网络异常")
        provider = type("FakeProvider", (), {"generate": raise_exc})()
        result, count, attempts = _post_extend_if_short(
            text=original, target_min=107, target_max=156,
            topic="时尚女鞋", provider_obj=provider,
        )
        self.assertEqual(result, original,
            "v3.16: provider 抛异常应保留原文本, 不向上抛")
        # 异常路径仍计 1 次 (实际调过 model, 只是抛了), 语义: 调用次数而非成功次数
        self.assertEqual(attempts, 1, "v3.16: 异常路径实际调过 1 次 model")


class PostCompressIfLongTests(unittest.TestCase):
    """长文案不再硬截断, 而是用模型压缩重写。"""

    def _make_provider(self, side_effects):
        call_count = [0]
        def generate(*args, **kwargs):
            idx = call_count[0]
            call_count[0] += 1
            if idx >= len(side_effects):
                return side_effects[-1]
            return side_effects[idx]
        provider = type("FakeProvider", (), {"generate": generate})()
        return provider, call_count

    def test_long_text_gets_compressed(self):
        long_text = (
            "时尚女鞋真的很适合日常穿搭, 不管通勤还是周末出门都能让整体造型更完整。"
            "它可以搭配不同风格的衣服, 让普通一天多一点轻松感和仪式感。"
        ) * 4
        compressed = (
            "时尚女鞋适合放进日常穿搭里, 通勤、逛街或周末出门都能自然衔接。"
            "它不用夸张表达, 就能让整体造型更完整, 也让普通一天多一点轻松感。"
            "选对一双合适的鞋, 出门前少一点纠结, 走路时也更自在。"
        )
        provider, call_count = self._make_provider([compressed])
        result, count, attempts = _post_compress_if_long(
            long_text, target_min=85, target_max=156,
            topic="时尚女鞋", provider_obj=provider,
        )
        self.assertEqual(attempts, 1)
        self.assertEqual(call_count[0], 1)
        self.assertNotEqual(result, long_text)
        self.assertLess(count, _content_char_count(long_text))
        self.assertLessEqual(count, int(156 * 1.15))

    def test_too_short_compression_rejected(self):
        long_text = "时尚女鞋适合日常穿搭, 通勤逛街都能让造型更完整。" * 8
        provider, call_count = self._make_provider(["太短了。"])
        result, count, attempts = _post_compress_if_long(
            long_text, target_min=85, target_max=156,
            topic="时尚女鞋", provider_obj=provider,
        )
        self.assertEqual(attempts, 1)
        self.assertEqual(result, long_text)
        self.assertGreater(count, 156)


# ── 端到端集成 (mock model 复现「模型+清洗双重偏差」) ─────────────


class E2EPostExtendIntegrationTests(unittest.TestCase):
    """v3.16 端到端: 复现「模型输出 70 字 + hashtag → 清洗后 60 字」场景."""

    def test_post_extend_rescues_short_cleaned_output(self):
        short_output = (
            "想为日常穿搭多一点灵感, 其实一双合适的鞋就能带来很大的变化。"
            "春夏季节穿上轻便的款式, 整体造型也跟着松弛自然起来。"
            "#经典单品 #品质保证"
        )
        extend_output = (
            "想为日常穿搭多一点灵感, 其实一双合适的鞋就能带来很大的变化。"
            "春夏季节穿上轻便的款式, 整体造型也跟着松弛自然起来。"
            "百搭的设计不挑任何风格, 通勤逛街约会出游都能轻松切换。"
            "用舒服的步调走出自己的味道, 让每个普通一天都多一点新鲜感。"
        )
        call_count = [0]
        prompts_seen = []

        def fake_generate(self, prompt, max_tokens):
            prompts_seen.append(prompt)
            idx = call_count[0]
            call_count[0] += 1
            if idx < 3:
                return short_output
            return extend_output

        with patch("autokat.core.ai_providers.build_writer_provider") as mock_factory:
            mock_factory.return_value = type(
                "FakeProvider", (), {"generate": fake_generate}
            )()
            try:
                result = generate_script_by_topic_detailed(
                    topic="时尚女鞋",
                    target_chars_min=107, target_chars_max=156,
                    max_attempts=3, provider="local",
                )
            except RuntimeError as e:
                self.fail(f"v3.16: post-extend 应救场, 不应 raise. err={e!r}")

        self.assertGreaterEqual(call_count[0], 4,
            f"v3.16: 主循环 3 + post-extend 1+ = 4 次, got {call_count[0]}")
        self.assertTrue(result["quality"]["valid"],
            f"v3.16: post-extend 后应 valid, reasons={result['quality']['reasons']}")
        self.assertGreaterEqual(_content_char_count(result["text"]), 107,
            f"v3.16: 最终文案应 >= 107 字, got {_content_char_count(result['text'])}")
        extend_prompt = prompts_seen[3] if len(prompts_seen) >= 4 else ""
        self.assertIn("只在末尾添加", extend_prompt,
            f"v3.16: 第 4 次调用应使用聚焦 extend prompt. got: {extend_prompt[:200]!r}")

    def test_in_range_output_does_not_trigger_post_extend(self):
        good_output = (
            "想为日常穿搭多一点灵感, 其实一双合适的鞋就能带来很大的变化。"
            "春夏季节穿上轻便的款式, 整体造型也跟着松弛自然起来, 走起路来都更有节奏感。"
            "百搭的设计不挑任何风格, 通勤逛街约会出游都能轻松切换。"
            "用舒服的步调走出自己的味道, 让每个普通一天都多一点新鲜感。"
        )
        call_count = [0]
        def fake_generate(self, prompt, max_tokens):
            call_count[0] += 1
            return good_output

        with patch("autokat.core.ai_providers.build_writer_provider") as mock_factory:
            mock_factory.return_value = type(
                "FakeProvider", (), {"generate": fake_generate}
            )()
            result = generate_script_by_topic_detailed(
                topic="时尚女鞋",
                target_chars_min=107, target_chars_max=156,
                max_attempts=3, provider="local",
            )

        self.assertEqual(call_count[0], 1,
            f"v3.16: 1 次就过, model 只应被调 1 次, got {call_count[0]}")
        self.assertTrue(result["quality"]["valid"])


# ── 根因复现: 「模型+清洗双重偏差」原版 user reported 案例 ───────


class RootCauseReproductionTests(unittest.TestCase):
    """复现用户报告的根因 — 验证模型/系统字数认知确实不一致."""

    def test_user_reported_case_75_chars_with_hashtag(self):
        text = (
            "想为日常穿搭多一点灵感, 其实一双合适的鞋就能带来很大的变化。"
            "春夏季节穿上轻便的款式, 整体造型也跟着松弛自然起来。"
            "走起路来都更有节奏感。#经典单品 #品质保证"
        )
        raw_count = len(text)
        clean = _clean_result(text, topic="时尚女鞋")
        clean_count = _content_char_count(clean)
        self.assertGreater(raw_count - clean_count, 5,
            f"v3.16: 模型/系统字数差距应 > 5, got {raw_count - clean_count}")
        self.assertLess(clean_count, 107,
            "v3.16: 75 字清洗后一定 < 107 (用户报告的失败场景)")

    def test_cleaner_eats_hashtag_emoji_markdown(self):
        polluted = (
            "一穿上就放不下。#经典单品 #品质保证 #值得拥有 🌟✨ "
            "**真的太好穿了** 【推荐】[字数:130] \n"
        )
        clean = _clean_result(polluted, topic="时尚女鞋")
        lost = _content_char_count(polluted) - _content_char_count(clean)
        self.assertGreater(lost, 15,
            f"v3.16: 典型污染输入应丢 > 15 字, got {lost}")


# ── 回归保护: 不破坏 v3.13 / v3.8 / v3.2 的既有行为 ──────────────


class RegressionTests(unittest.TestCase):
    """v3.16 回归保护: 旧行为不能被破坏."""

    def test_no_safe_template_source_in_writer(self):
        import inspect
        from autokat.core import writer
        src = inspect.getsource(writer)
        self.assertNotIn('"安全模板"', src)
        self.assertNotIn("'安全模板'", src)

    def test_unrelated_output_3_retries_then_raises(self):
        def fake_generate(self, prompt, max_tokens):
            return "今天天气真好, 我们来聊聊生活。"
        with patch("autokat.core.ai_providers.build_writer_provider") as mock_factory:
            mock_factory.return_value = type(
                "FakeProvider", (), {"generate": fake_generate}
            )()
            with self.assertRaises(RuntimeError) as ctx:
                generate_script_by_topic_detailed(
                    "时尚女鞋", "种草推荐",
                    target_chars_min=107, target_chars_max=142,
                    max_attempts=3,
                )
        err = str(ctx.exception)
        self.assertIn("手动录入", err,
            "v3.16 回归: AI 失败仍应 raise 提示手动录入 (v3.2 行为不变)")
        self.assertTrue(
            "字数不足" in err or "未围绕选题" in err,
            f"v3.16: 异常应含主阶段失败原因, got: {err[:200]}")


if __name__ == "__main__":
    unittest.main()
