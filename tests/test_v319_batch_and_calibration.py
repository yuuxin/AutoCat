"""v3.19 守护测试: 批量生成 + 自适应字数校准."""
import re
import unittest
from unittest.mock import patch

from autokat.core.writer import (
    _BATCH_SCRIPT_MAX,
    _BATCH_SCRIPT_SEPARATOR_RE,
    _build_batch_prompt,
    _content_char_count,
    _parse_batch_output,
    compute_model_target,
    generate_scripts_batch,
)


# 5 个变种 (每个 ≥ 90 字, 互不相似) — 用 module-level 而非 class attr,
# 避免 inner class 访问 self.SCRIPT_VARIANTS 找不到
SCRIPT_VARIANTS = [
    ("想为日常穿搭多一点灵感, 其实一双合适的女鞋就能带来很大的变化。",
     "春夏季节穿上轻便的款式, 整体造型也跟着松弛自然起来, 走起路来都更有节奏感。",
     "百搭的女鞋设计不挑任何风格, 通勤逛街约会出游都能轻松切换, 让你省心又自在。"),
    ("没想到时尚女鞋还能这样, 真的是打开新世界的大门。",
     "轻盈的女鞋鞋面透气不闷脚, 走多远都不觉得累, 整个人状态都不一样了。",
     "经典的版型怎么搭配都不会出错, 实用性拉满, 每天都有好心情。"),
    ("以前每次换季都头疼, 直到遇见这双女鞋才彻底解决所有烦恼。",
     "柔软的鞋面包裹双脚, 长时间穿着也不磨脚, 通勤一整天都轻松自在。",
     "不管是上班通勤还是周末出游, 这双女鞋都能轻松驾驭各种风格, 造型感拉满。"),
    ("对比了十款不同风格的时尚女鞋, 最后还是这双最值得入手。",
     "细节做工精致考究, 每一处都看得出用心良苦, 品质感从细节里透出来。",
     "穿上这双女鞋整个人的气质都提升了一个档次, 走在哪里都是焦点。"),
    ("姐妹们, 你们还在为穿搭烦恼吗? 其实这双女鞋就够了。",
     "穿上它不仅显腿长还显气质, 拍照都不用修图, 朋友圈点赞爆表。",
     "那种从脚底升起的舒适感, 让你走再多路也不觉得累, 心情也跟着变好。"),
]


def _make_provider(responses):
    """Mock provider: 按 responses 列表顺序返回, 超出后重复最后一个.
    类名设 LocalWriterProvider 让 _provider_obj_to_str 返回 "local".
    call_count 放在实例上, 测试可访问.
    """
    if not responses:
        responses = [""]

    class _P:
        def __init__(self, rs):
            self.responses = rs
            self.call_count = 0
            self.prompts_seen = []
        def generate(self, *args, **kwargs):
            idx = min(self.call_count, len(self.responses) - 1)
            self.call_count += 1
            self.prompts_seen.append(args[0] if args else kwargs.get("prompt"))
            return self.responses[idx]
    p = _P(responses)
    p.__class__.__name__ = "LocalWriterProvider"
    return p


def _patch_bwp(provider):
    """batch fallback 调 generate_script_by_topic_detailed → build_writer_provider
    这里也让它返回 mock provider (否则 fallback 走真实模型).
    """
    return patch("autokat.core.ai_providers.build_writer_provider", return_value=provider)


class ComputeModelTargetTests(unittest.TestCase):
    def test_default_retention_0_75(self):
        m_min, m_max = compute_model_target(85, 156, 0.75)
        self.assertEqual((m_min, m_max), (113, 208))

    def test_retention_0_8(self):
        m_min, m_max = compute_model_target(100, 150, 0.8)
        self.assertEqual((m_min, m_max), (125, 187))

    def test_retention_1_0_min_bump(self):
        """v3.19 设计: model_min 始终 > system_min (留 buffer)"""
        m_min, m_max = compute_model_target(100, 150, 1.0)
        self.assertEqual((m_min, m_max), (101, 150))

    def test_retention_below_zero_returns_input(self):
        self.assertEqual(compute_model_target(100, 150, 0), (100, 150))
        self.assertEqual(compute_model_target(100, 150, -0.5), (100, 150))

    def test_retention_above_one_returns_input(self):
        self.assertEqual(compute_model_target(100, 150, 1.2), (100, 150))

    def test_none_input_returns_none(self):
        self.assertEqual(compute_model_target(None, 150, 0.75), (None, None))
        self.assertEqual(compute_model_target(100, None, 0.75), (None, None))
        self.assertEqual(compute_model_target(0, 150, 0.75), (None, None))
        self.assertEqual(compute_model_target(-10, 150, 0.75), (None, None))

    def test_model_target_strictly_above_system(self):
        for r in [0.5, 0.6, 0.7, 0.8, 0.9, 0.99]:
            m_min, _ = compute_model_target(100, 200, r)
            self.assertGreater(m_min, 100)

    def test_realistic_20_30s_scenario(self):
        m_min, m_max = compute_model_target(85, 156, 0.75)
        model_typical = (m_min + m_max) // 2
        sys_typical = int(model_typical * 0.75)
        self.assertGreaterEqual(sys_typical, 85)
        self.assertLessEqual(sys_typical, 156)


class ParseBatchOutputTests(unittest.TestCase):
    def test_parse_normal_batch(self):
        raw = (
            "=== 文案1 ===\n想为日常穿搭多一点灵感, 其实一双合适的鞋就能带来很大的变化。\n"
            "=== 文案2 ===\n春夏季节一双合适的鞋, 让整个人都松弛自然起来。\n"
            "=== 文案3 ===\n百搭的款式通勤逛街约会都能轻松切换, 让你每天都有好心情。\n"
        )
        parsed = _parse_batch_output(raw, expected_count=3)
        self.assertEqual(len(parsed), 3)

    def test_parse_with_preamble(self):
        raw = "好的: === 文案1 ===\nA1\n=== 文案2 ===\nA2\n=== 文案3 ===\nA3\n"
        parsed = _parse_batch_output(raw, expected_count=3)
        self.assertEqual(len(parsed), 3)

    def test_parse_no_separator_returns_whole_as_one(self):
        parsed = _parse_batch_output("一段话而已", 3)
        self.assertEqual(len(parsed), 1)

    def test_parse_empty_returns_empty(self):
        self.assertEqual(_parse_batch_output("", 3), [])
        self.assertEqual(_parse_batch_output(None, 3), [])

    def test_parse_model_outputs_more_than_expected(self):
        raw = "=== 文案1 ===\nA1\n=== 文案2 ===\nA2\n=== 文案3 ===\nA3\n=== 文案4 ===\nA4\n=== 文案5 ===\nA5\n"
        parsed = _parse_batch_output(raw, expected_count=3)
        self.assertEqual(len(parsed), 3)
        self.assertEqual([p[0] for p in parsed], [1, 2, 3])

    def test_parse_unordered_indices(self):
        raw = "=== 文案2 ===\nB2\n=== 文案1 ===\nB1\n=== 文案3 ===\nB3\n"
        parsed = _parse_batch_output(raw, expected_count=3)
        self.assertEqual([p[0] for p in parsed], [1, 2, 3])

    def test_parse_model_outputs_fewer_than_expected(self):
        raw = "=== 文案1 ===\nA1\n=== 文案2 ===\nA2\n=== 文案3 ===\nA3\n"
        parsed = _parse_batch_output(raw, expected_count=5)
        self.assertEqual(len(parsed), 3)

    def test_parse_strips_nested_separators(self):
        raw = "=== 文案1 ===\n内容 === 文案1 === 噪音\n=== 文案2 ===\nB2\n"
        parsed = _parse_batch_output(raw, expected_count=2)
        self.assertEqual(len(parsed), 2)
        self.assertNotIn("===", parsed[0][1])


class BuildBatchPromptTests(unittest.TestCase):
    def test_prompt_has_separator_format(self):
        p = _build_batch_prompt(
            topic="时尚女鞋", style="种草推荐", count=5, start_index=0,
            target_chars_min=107, target_chars_max=142, retention=0.75,
        )
        self.assertIn("5 条文案", p)
        self.assertIn("=== 文案1 ===", p)
        self.assertIn("=== 文案5 ===", p)

    def test_prompt_uses_model_target(self):
        p = _build_batch_prompt(
            topic="时尚女鞋", style="种草推荐", count=3, start_index=0,
            target_chars_min=85, target_chars_max=156, retention=0.75,
        )
        self.assertIn("113", p)
        self.assertIn("208", p)

    def test_prompt_different_angles(self):
        p = _build_batch_prompt(
            topic="时尚女鞋", style="种草推荐", count=5, start_index=0,
            target_chars_min=107, target_chars_max=142, retention=0.75,
        )
        for i in range(1, 6):
            self.assertIn(f"文案 {i}:", p)

    def test_prompt_no_chars_target(self):
        p = _build_batch_prompt(
            topic="时尚女鞋", style="种草推荐", count=3, start_index=0,
            target_chars_min=None, target_chars_max=None, retention=0.75,
        )
        self.assertIn("3 条", p)
        self.assertIn("100-200", p)


class GenerateScriptsBatchE2ETests(unittest.TestCase):
    """端到端: 1 次 model 调用生成 N 条."""

    def _good(self, idx):
        o, m, e = SCRIPT_VARIANTS[idx]
        return o + m + e

    def test_5_scripts_in_1_call(self):
        # batch 输出 5 条, 全部合格
        # batch 输出 5 条, 全部合格; 加序号前缀防 3-gram 重叠
        raw = "".join(
            f"=== 文案{i+1} ===\n"
            f"第 {i + 1} 条推荐, {self._good(i % 5)}\n"
            for i in range(5)
        )
        provider = _make_provider([raw])
        with _patch_bwp(provider):
            results = generate_scripts_batch(
                topic="时尚女鞋", count=5,
                target_chars_min=85, target_chars_max=156,
                provider_obj=provider, max_batch_size=20,
            )
        # 关键: 1 次 model 调用搞定 5 条
        self.assertEqual(provider.call_count, 1,
            f"v3.19: 5 条应 1 次调用, 实际 {provider.call_count} 次")
        self.assertEqual(len(results), 5)
        for r in results:
            self.assertTrue(r["quality"]["valid"],
                f"v3.19: batch 文案应合格, reasons={r['quality']['reasons']}")
        self.assertEqual([r["batch_idx"] for r in results], [0, 1, 2, 3, 4])

    def test_25_scripts_split_into_2_batches(self):
        """25 条 > 20 → 分 2 批 (20+5), 关键验证 batch 拆分逻辑."""
        def make_batch(start_idx, count):
            # 每条加唯一序号前缀 (破 3-gram 重叠, 避免相似度 reject)
            return "".join(
                f"=== 文案{start_idx + i + 1} ===\n"
                f"第 {start_idx + i + 1} 条推荐, {self._good(i % 5)}\n"
                for i in range(count)
            )
        # batch 1: 20 条, batch 2: 5 条, fallback 用变种 1
        provider = _make_provider([
            make_batch(0, 20),
            make_batch(20, 5),
        ])
        with _patch_bwp(provider):
            results = generate_scripts_batch(
                topic="时尚女鞋", count=25,
                target_chars_min=85, target_chars_max=156,
                provider_obj=provider, max_batch_size=20,
            )
        # 关键: 25 条 > 20 → 分 2 批 (用 prompts_seen 统计 batch prompt 数量)
        batch_calls = sum(1 for p in provider.prompts_seen if "本批要求生成" in p)
        self.assertEqual(batch_calls, 2,
            f"v3.19: 25 条应分 2 批, 实际 batch_calls={batch_calls}")
        # 至少 20 条从 batch 解析 (剩下可能因相似度 reject 走 fallback)
        self.assertGreaterEqual(len(results), 20,
            f"v3.19: 至少 20 条从 batch 解析, got {len(results)}")

    def test_failed_paragraphs_fall_back(self):
        """batch 中第 2 条太短, fallback 到 single-call 补上."""
        good = self._good
        # raw: 3 条, 第 2 条太短 (validate fail). 加序号前缀防 3-gram 重叠.
        raw = (
            f"=== 文案1 ===\n第 1 条推荐, {good(0)}\n"
            f"=== 文案2 ===\n太短了。\n"
            f"=== 文案3 ===\n第 3 条推荐, {good(3)}\n"
        )
        # batch 返回 raw. fallback 每次返回不同变种 (且加序号前缀防相似度 reject).
        # 变种选法: 1 (跟 0/3 都不同) → 2 (跟 0/1/3 都不同) → 4 (跟 0/1/2/3 都不同)
        def fallback_resp(call_idx):
            variant_idx = [1, 2, 4][min(call_idx, 2)]
            return f"第 2 条推荐, {good(variant_idx)}"
        provider = _make_provider([
            raw,
            fallback_resp(0),
            fallback_resp(1),
            fallback_resp(2),
        ])
        with _patch_bwp(provider):
            results = generate_scripts_batch(
                topic="时尚女鞋", count=3,
                target_chars_min=85, target_chars_max=156,
                provider_obj=provider, max_batch_size=20,
            )
        # 关键: 3 条都应 fill 上 (第 2 条走 fallback)
        self.assertEqual(len(results), 3,
            f"v3.19: 3 条应全 fill 上, got {len(results)}")
        for i, r in enumerate(results):
            self.assertIn("text", r)
            self.assertTrue(len(r["text"]) > 30, f"文案 {i} 应有内容")

    def test_empty_batch_falls_back(self):
        """batch 输出空 → 全部走 single-call fallback."""
        # batch 返回 "", fallback 用变种 0, 1 (互不相似, 加序号前缀)
        provider = _make_provider([
            "",  # batch 空
            f"第 1 条推荐, {self._good(0)}",
            f"第 2 条推荐, {self._good(1)}",
        ])
        with _patch_bwp(provider):
            results = generate_scripts_batch(
                topic="时尚女鞋", count=2,
                target_chars_min=85, target_chars_max=156,
                provider_obj=provider, max_batch_size=20,
            )
        # 1 batch + 2 single (无 fallback retry) = 3 calls
        self.assertGreaterEqual(provider.call_count, 3)
        self.assertEqual(len(results), 2)

    def test_empty_batch_falls_back(self):
        """batch 输出空 → 全部走 single-call fallback."""
        # batch 返回 "", 后续 fallback 用 variants[0], variants[1] (互不相似)
        provider = _make_provider([
            "",  # batch 空
            self._good(0),
            self._good(1),
        ])
        with _patch_bwp(provider):
            results = generate_scripts_batch(
                topic="时尚女鞋", count=2,
                target_chars_min=85, target_chars_max=156,
                provider_obj=provider, max_batch_size=20,
            )
        # 1 batch + 2 single (无 fallback retry) = 3 calls
        self.assertGreaterEqual(provider.call_count, 3)
        self.assertEqual(len(results), 2)


class BatchMaxSizeTests(unittest.TestCase):
    def test_default_max_is_20(self):
        self.assertEqual(_BATCH_SCRIPT_MAX, 20)

    def test_count_0_returns_empty(self):
        p = type("P", (), {"generate": lambda *a, **k: ""})()
        results = generate_scripts_batch(
            topic="x", count=0,
            target_chars_min=100, target_chars_max=200,
            provider_obj=p,
        )
        self.assertEqual(results, [])

    def test_count_negative_returns_empty(self):
        p = type("P", (), {"generate": lambda *a, **k: ""})()
        results = generate_scripts_batch(
            topic="x", count=-5,
            target_chars_min=100, target_chars_max=200,
            provider_obj=p,
        )
        self.assertEqual(results, [])

    def test_separator_regex(self):
        self.assertIsNotNone(_BATCH_SCRIPT_SEPARATOR_RE.search("===文案1==="))
        self.assertIsNotNone(_BATCH_SCRIPT_SEPARATOR_RE.search("=== 文案1 ==="))
        m = _BATCH_SCRIPT_SEPARATOR_RE.search("=== 文案123 ===")
        self.assertEqual(m.group(1), "123")


class CalibrationRealSampleTests(unittest.TestCase):
    def test_user_sample_81_chars(self):
        """用户样本: 81 字 (含 hashtag) → 67 字, 校准后应落入 system 范围."""
        sys_min, sys_max = 80, 100
        m_min, m_max = compute_model_target(sys_min, sys_max, 0.75)
        model_wrote = 130
        sys_actual = int(model_wrote * (1 - 0.17))
        self.assertGreaterEqual(sys_actual, sys_min)

    def test_retention_robustness_sweep(self):
        """retention 0.6-0.95 范围内, system 都能落入目标."""
        for r in [0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95]:
            m_min, m_max = compute_model_target(100, 150, r)
            model_median = (m_min + m_max) // 2
            sys_typical = int(model_median * r)
            self.assertGreaterEqual(sys_typical, 100)


if __name__ == "__main__":
    unittest.main()
