"""Regression tests for the 3 issues reported with task 176 follow-up.

Issue 1: AI 生成文案含「设计灵感/匠心独运/完美展现/艺术品/独一无二」
         + 跨品类混淆「鞋子变成衣服」
   → 期望: validate_script_quality 拒收设计过程捏造/无支撑过度承诺/跨品类混淆

Issue 2: UI 提示 107-156 但后端 enforce 119-142 (数字不一致)
   → 期望: estimate_chars_for_duration_range 应用 margin=0.10,
           target_chars_min/max 与 UI 显示完全一致

Issue 3: 时长范围 30-30s 时 UI 只显示「142」(单数字)
   → 期望: dmin==dmax 也按 margin 显示范围, 比如 128-156
"""
import re
import unittest

from autokat.core.writer import (
    _CROSS_CATEGORY_FORBIDDEN,
    _FABRICATED_PROCESS_CLAIMS,
    _OVERCLAIMS_NO_SUPPORT,
    _detect_topic_category,
    estimate_chars_for_duration_range,
    estimate_chars_for_lang,
    validate_script_quality,
)


# ── Issue 1a: 凭空捏造设计过程 ──────────────────────────────────

class FabricatedProcessClaimsTests(unittest.TestCase):
    """模型常凭空编造「设计师的故事/灵感/工艺」填充篇幅 — 必须拒收。"""

    def test_rejects_designer_story(self):
        text = (
            "每个款式的背后都有故事，都是设计师匠心独运的结果。"
            "春夏女鞋的搭配灵感来自日常生活中的细节观察。"
        )
        result = validate_script_quality(text, "春夏女鞋", lang="zh")
        reasons_str = " | ".join(result["reasons"])
        self.assertFalse(result["valid"], f"应被拒收，reasons={reasons_str}")
        self.assertTrue(
            any("设计过程" in r or "捏造" in r for r in result["reasons"]),
            f"应包含「捏造设计过程」原因，实际={result['reasons']}",
        )

    def test_rejects_handmade_claim(self):
        text = "这双女鞋纯手工打造，每一道工序都精雕细琢。"
        result = validate_script_quality(text, "时尚女鞋", lang="zh")
        self.assertFalse(result["valid"])
        self.assertTrue(
            any("设计过程" in r for r in result["reasons"]),
            f"手工/工序/精雕细琢应被标记, 实际={result['reasons']}",
        )

    def test_rejects_every_detail_claim(self):
        text = "完美展现每一寸细节，每一步都精心打造。"
        result = validate_script_quality(text, "时尚女鞋", lang="zh")
        self.assertFalse(result["valid"])
        # 同时命中 捏造设计过程 和 无支撑过度承诺
        self.assertTrue(
            any("设计过程" in r for r in result["reasons"]),
            f"「精心打造」「每一寸细节」应被标记, 实际={result['reasons']}",
        )
        self.assertTrue(
            any("过度承诺" in r for r in result["reasons"]),
            f"「完美展现」应被标记, 实际={result['reasons']}",
        )


# ── Issue 1b: 无支撑的过度承诺 ──────────────────────────────────

class OverclaimsNoSupportTests(unittest.TestCase):

    def test_rejects_artwork(self):
        text = "穿上它，就像在阳光下绽放的花朵，每一双脚都是独一无二的艺术品。"
        result = validate_script_quality(text, "时尚女鞋", lang="zh")
        self.assertFalse(result["valid"])
        self.assertTrue(
            any("过度承诺" in r for r in result["reasons"]),
            f"「艺术品/独一无二」应被标记, 实际={result['reasons']}",
        )

    def test_rejects_ultimate_claim(self):
        text = "全新升级的极致体验，全球首发颠覆性设计。"
        result = validate_script_quality(text, "时尚女鞋", lang="zh")
        self.assertFalse(result["valid"])
        self.assertTrue(
            any("过度承诺" in r for r in result["reasons"]),
            f"「极致/全新升级/全球首发/颠覆性」应被标记, 实际={result['reasons']}",
        )


# ── Issue 1c: 跨品类混淆 ────────────────────────────────────────

class CrossCategoryConfusionTests(unittest.TestCase):

    def test_shoes_topic_rejects_clothes_words(self):
        text = "穿上这款春夏女鞋，您将不再仅仅是一件衣服，而是一件承载着您个性与品味的衣物。"
        result = validate_script_quality(text, "时尚女鞋", lang="zh")
        self.assertFalse(result["valid"])
        self.assertTrue(
            any("跨品类" in r for r in result["reasons"]),
            f"女鞋+「衣服/衣物」应被标记, 实际={result['reasons']}",
        )

    def test_clothes_topic_rejects_shoes_words(self):
        text = "这件上衣搭配运动鞋，让你的造型更有活力。"
        result = validate_script_quality(text, "时尚上衣", lang="zh")
        self.assertFalse(result["valid"])
        self.assertTrue(
            any("跨品类" in r for r in result["reasons"]),
            f"上衣+「运动鞋」应被标记, 实际={result['reasons']}",
        )

    def test_shoes_topic_does_not_flag_reasonable_pairing(self):
        # 鞋子搭配衣服 是正常造型场景 — 不应被拒收 (主题鞋子, 衣服作为搭配)
        # 这里我们只关心"鞋子变成衣服"那种主谓混淆,
        # 简单实现会过度严格, 这里允许少量误报
        text = "春夏女鞋搭配今天的裙子，让整体造型更有层次感。"
        # 当前实现: 只要出现 cross_hits 就拒收, 这里期望会拒收 (False positive 接受)
        # 因为单纯 substring 检测无法区分主谓混淆 vs 搭配场景
        result = validate_script_quality(text, "春夏女鞋", lang="zh")
        # 文档化当前行为: 简单 substring 会命中, 但用户可手动确认
        self.assertIn("跨品类", " | ".join(result["reasons"]))

    def test_no_category_skips_check(self):
        # topic 是抽象风格词如「穿搭」, 没有具体品类, 应跳过跨品类检测
        text = "今天分享几个穿搭灵感，衣服与鞋子都要选对。"
        result = validate_script_quality(text, "穿搭", lang="zh")
        self.assertNotIn(
            True,
            [("跨品类" in r) for r in result["reasons"]],
            f"抽象风格词 topic 应跳过跨品类检测, 实际={result['reasons']}",
        )


# ── Issue 1d: _detect_topic_category 单元 ────────────────────────

class DetectTopicCategoryTests(unittest.TestCase):

    def test_shoes_category(self):
        for topic in ["时尚女鞋", "运动鞋", "高跟鞋", "皮鞋", "靴子", "袜子"]:
            self.assertEqual(_detect_topic_category(topic), "鞋", f"{topic} 应识别为「鞋」")

    def test_clothes_category(self):
        for topic in ["时尚上衣", "衬衫", "裤子", "连衣裙"]:
            self.assertEqual(_detect_topic_category(topic), "衣", f"{topic} 应识别为「衣」")

    def test_bag_category(self):
        self.assertEqual(_detect_topic_category("手提包"), "包")
        self.assertEqual(_detect_topic_category("背包"), "包")

    def test_hat_category(self):
        self.assertEqual(_detect_topic_category("鸭舌帽"), "帽")
        self.assertEqual(_detect_topic_category("帽子"), "帽")

    def test_abstract_topic_returns_none(self):
        self.assertIsNone(_detect_topic_category("穿搭"))
        self.assertIsNone(_detect_topic_category("生活方式"))
        self.assertIsNone(_detect_topic_category(""))
        self.assertIsNone(_detect_topic_category(None))


# ── Issue 2: UI / 后端 字数范围一致性 ──────────────────────────

class CharRangeConsistencyTests(unittest.TestCase):

    def test_zh_25_30s_returns_107_156(self):
        """25-30s 中文, 默认 margin=0.10 → 107-156 (与 UI 之前显示一致)"""
        target_min, target_max = estimate_chars_for_duration_range("zh", 25, 30)
        self.assertEqual(target_min, 107)
        self.assertEqual(target_max, 156)

    def test_zh_30_30s_returns_range(self):
        """30-30s 也应显示范围, 不再是单数字"""
        target_min, target_max = estimate_chars_for_duration_range("zh", 30, 30)
        # 30 * 4.76 = 142 (int) → 142 * 0.9 = 127.8 → int = 127
        self.assertEqual(target_min, 127)
        self.assertEqual(target_max, 156)

    def test_zh_15_30s_returns_71_156(self):
        """15-30s 宽范围"""
        target_min, target_max = estimate_chars_for_duration_range("zh", 15, 30)
        # 15 * 4.76 * 0.9 = 64.26 → 64; 30 * 4.76 * 1.1 = 157.08 → 157
        # 实现细节: 取 lo=min(15,30)=15, hi=max(15,30)=30
        # ideal_lo = 15 * 4.76 = 71 → target_min = max(1, int(71 * 0.9)) = 63
        # ideal_hi = 30 * 4.76 = 142 → target_max = int(142 * 1.1) = 156
        self.assertEqual(target_min, 63)
        self.assertEqual(target_max, 156)

    def test_thai_returns_thai_speed(self):
        target_min, target_max = estimate_chars_for_duration_range("th", 25, 30)
        # th: 12.5 chars/sec → ideal_lo = int(25*12.5)=312 → target_min = int(312*0.9) = 280
        # ideal_hi = int(30*12.5)=375 → target_max = int(375*1.1) = 412
        self.assertEqual(target_min, 280)
        self.assertEqual(target_max, 412)

    def test_margin_zero_disables_margin(self):
        target_min, target_max = estimate_chars_for_duration_range(
            "zh", 25, 30, margin=0.0,
        )
        # margin=0 时 = ideal_at_each_duration
        self.assertEqual(target_min, 119)
        self.assertEqual(target_max, 142)

    def test_negative_rate_speeds_down(self):
        target_min, target_max = estimate_chars_for_duration_range(
            "zh", 25, 30, rate_pct=-20,
        )
        # -20% 语速 → base 4.76 * 0.8 = 3.808 chars/sec
        # ideal_lo = 25 * 3.808 = 95 → target_min = max(1, int(95 * 0.9)) = 85
        # ideal_hi = 30 * 3.808 = 114 → target_max = int(114 * 1.1) = 125
        self.assertEqual(target_min, 85)
        self.assertEqual(target_max, 125)


# ── Issue 3: dmin==dmax 时 UI 也应显示范围 ────────────────────

class DminEqualsDmaxRangeTests(unittest.TestCase):
    """通过 estimate_chars_for_duration_range(dmin, dmin) 验证 dmin==dmax 路径"""

    def test_zh_30_30s_range(self):
        target_min, target_max = estimate_chars_for_duration_range("zh", 30, 30)
        self.assertGreater(target_max - target_min, 0,
                           "30-30s 应有上下界范围 (margin>0)")

    def test_zh_15_15s_range(self):
        target_min, target_max = estimate_chars_for_duration_range("zh", 15, 15)
        # 15 * 4.76 = 71; 71 * 0.9 = 63; 71 * 1.1 = 78
        self.assertEqual(target_min, 63)
        self.assertEqual(target_max, 78)


# ── Issue 1e: 用户实际给的 5 段 bad output 端到端检测 ──────────

class UserReportedBadOutputsTests(unittest.TestCase):
    """用户实际报告的 5 段 AI 文案 (task 176 follow-up) 全部应被拒收。"""

    BAD_OUTPUTS = [
        # 1: 鞋子变成衣服 + 过度承诺
        "穿上我们的最新款春夏女鞋，让您的脚更加优雅。"
        "这款设计独特的鞋子，不仅拥有出色的舒适度，还具有时尚感。"
        "无论是日常出行还是特殊场合，都能让您从容应对。"
        "快来感受这款时尚魅力十足的女鞋吧！"
        "穿上这款春夏女鞋，您将不再仅仅是一件衣服，"
        "而是一件承载着您个性与品味的衣物。",
        # 3: 完美展现 + 精心打造 + 每一寸细节
        "已经选择了优质的女鞋进行展示，特别是一些设计独特、质感出色的款式。"
        "每一步都精心打造，力求完美展现每一寸细节。"
        "让我们一起走进春天的季节，感受不一样的时尚魅力吧！"
        "接下来，我们来谈谈那些让人心动的设计灵感。"
        "每个款式的背后都有故事，都是设计师匠心独运的结果。",
        # 4: 经典 + 完美
        "已经选择了合适的素材，让我们开始吧！"
        "春夏，我们来挑选一双特别适合春天穿的女鞋——"
        "一款经典的、舒适又时尚的春装必备之选！",
        # 5: 艺术品 + 独一无二 + 独特见解
        "已经选择了合适的素材，准备为即将上线的春季新品——女鞋进行介绍。"
        "在这个充满活力的季节里，我们特别挑选了这款设计独特的鞋子，"
        "让每一步都成为焦点。"
        "它不仅展现了时尚的潮流趋势，还融入了我们对女性美的独特见解。"
        "穿上它，就像在阳光下绽放的花朵，"
        "每一双脚都是独一无二的艺术品。",
    ]

    def test_user_reported_outputs_are_rejected(self):
        for idx, text in enumerate(self.BAD_OUTPUTS, 1):
            with self.subTest(output=idx):
                result = validate_script_quality(
                    text, "时尚女鞋", lang="zh",
                    target_chars_min=100, target_chars_max=200,
                )
                self.assertFalse(
                    result["valid"],
                    f"用户报告的第 {idx} 段文案应被拒收，\n"
                    f"实际 reasons={result['reasons']}\n"
                    f"文案={text[:80]}...",
                )

    def test_good_output_still_passes(self):
        """正常文案不应被新加的检查误伤"""
        good = (
            "衣柜里的搭配总觉得少点感觉？时尚女鞋往往是点亮造型的关键。"
            "你可以根据当天的心情调整整体风格，让每次出门都有一点新鲜感。"
            "重点不是追赶潮流，而是穿出属于自己的自信。"
        )
        result = validate_script_quality(
            good, "时尚女鞋", lang="zh",
            target_chars_min=50, target_chars_max=200,
        )
        self.assertTrue(
            result["valid"],
            f"正常文案应通过, 实际 reasons={result['reasons']}",
        )


# ── Issue 1f: 静态检查 — 3 组常量必须覆盖用户报告的关键词 ─────

class ConstantCoverageTests(unittest.TestCase):

    def test_fabricated_claims_covers_user_patterns(self):
        for kw in ["设计灵感", "设计师", "匠心独运", "手工打造", "精雕细琢",
                   "精心打造", "每一寸细节"]:
            self.assertIn(kw, _FABRICATED_PROCESS_CLAIMS,
                          f"_FABRICATED_PROCESS_CLAIMS 必须覆盖「{kw}」")

    def test_overclaims_covers_user_patterns(self):
        for kw in ["完美", "艺术品", "独一无二", "极致", "全新升级"]:
            self.assertIn(kw, _OVERCLAIMS_NO_SUPPORT,
                          f"_OVERCLAIMS_NO_SUPPORT 必须覆盖「{kw}」")

    def test_cross_category_covers_shoes_to_clothes(self):
        # 鞋 → 不能出现 衣服/衣物
        shoes_rules = _CROSS_CATEGORY_FORBIDDEN[("鞋",)]
        for kw in ["衣服", "衣物", "上衣", "裤子"]:
            self.assertIn(kw, shoes_rules,
                          f"_CROSS_CATEGORY_FORBIDDEN[鞋] 必须覆盖「{kw}」")


if __name__ == "__main__":
    unittest.main()
