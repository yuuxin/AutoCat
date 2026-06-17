"""v3.20 守护测试: 放宽 validation — 删 _FABRICATED_PROCESS_CLAIMS + 促销词阈值 >3.

用户反馈 (任务 568/753):
  - batch 5 条文案, 2 条因 validation fail (retry 3 次 + post-extend 都救不回)
  - AI 写「精挑细选 / 每一处细节 / 设计师推荐」是自然种草话术
  - 12 个黑名单词在 AI 自然文案中极常出现
  - 用户: 「要求太严格了 是不是可以去掉」

v3.20 修复:
  1. _FABRICATED_PROCESS_CLAIMS 整体清空 (19 词 → 0 词)
  2. _PROMOTION_WORDS 堆叠阈值 > 2 → > 3 (容许 1 个促销段不拒)
  3. 保留 _OVERCLAIMS_NO_SUPPORT (3 词) / _UNSUPPORTED_PRODUCT_CLAIMS / _CROSS_CATEGORY
"""
import unittest

from autokat.core.writer import (
    _FABRICATED_PROCESS_CLAIMS,
    _OVERCLAIMS_NO_SUPPORT,
    validate_script_quality,
)


class V320RelaxedBlacklistTests(unittest.TestCase):
    """v3.20: 黑名单放宽."""

    def test_fabricated_process_claims_disabled(self):
        """v3.20 核心: _FABRICATED_PROCESS_CLAIMS 整体禁用 (0 词)."""
        self.assertEqual(len(_FABRICATED_PROCESS_CLAIMS), 0,
            "v3.20: _FABRICATED_PROCESS_CLAIMS 应整体禁用 (0 词)")

    def test_natural_fabrication_words_no_longer_rejected(self):
        """v3.20: 自然种草话术不再 reject."""
        for kw in ["精挑细选", "每一处细节", "设计师推荐", "反复打磨",
                   "千锤百炼", "设计灵感", "设计故事", "精雕细琢",
                   "手工打造", "纯手工", "精心打造"]:
            text = f"这双女鞋{kw}, 穿上真的不一样。百搭的款式让你每天都有好心情。"
            text = (f"这双女鞋{kw}, 穿上真的不一样, 让你每次出门都有好心情。"
                    "百搭的款式让你每天都有好心情, 不挑任何风格, 通勤逛街约会都能轻松切换。")
            r = validate_script_quality(text, "时尚女鞋", lang="zh",
                                         target_chars_min=60, target_chars_max=200)
            self.assertTrue(r["valid"],
                f"v3.20: '{kw}' 应放行, reasons={r.get('reasons')}")

    def test_jiangxin_still_passes_v317(self):
        """v3.17 兼容: 匠心 仍放行."""
        r = validate_script_quality("这份匠心让女鞋更有温度。",
                                     "时尚女鞋", lang="zh",
                                     target_chars_min=10, target_chars_max=200)
        self.assertTrue(r["valid"], f"匠心 应放行, reasons={r.get('reasons')}")


class V320PromoStackingTests(unittest.TestCase):
    """v3.20: _PROMOTION_WORDS 堆叠阈值 > 2 → > 3."""

    def test_promo_stacking_2_passes(self):
        """v3.20: 2 个促销词不拒 (阈值放宽)."""
        text = "限时特惠, 这双女鞋真的太值了。百搭的款式让你每天都有好心情。"
        r = validate_script_quality(text, "时尚女鞋", lang="zh",
                                     target_chars_min=80, target_chars_max=200)
        reasons = [x for x in r.get("reasons", []) if "促销词" in x]
        self.assertEqual(reasons, [],
            f"v3.20: 2 个促销词不应 reject, reasons={reasons}")

    def test_promo_stacking_3_passes(self):
        """v3.20: 3 个促销词也不拒 (新阈值 > 3)."""
        text = "限时特惠, 抢购从速! 这双女鞋真的太值了。百搭的款式让你每天都有好心情。"
        r = validate_script_quality(text, "时尚女鞋", lang="zh",
                                     target_chars_min=80, target_chars_max=200)
        reasons = [x for x in r.get("reasons", []) if "促销词" in x]
        self.assertEqual(reasons, [],
            f"v3.20: 3 个促销词应放行 (阈值 > 3), reasons={reasons}")

    def test_promo_stacking_4_rejected(self):
        """v3.20: 4 个促销词才 reject (新阈值)."""
        text = ("限时特惠, 抢购从速, 折扣秒杀! 这双女鞋真的太值了。"
                "百搭的款式让你每天都有好心情。")
        r = validate_script_quality(text, "时尚女鞋", lang="zh",
                                     target_chars_min=80, target_chars_max=200)
        reasons = [x for x in r.get("reasons", []) if "促销词" in x]
        self.assertGreater(len(reasons), 0,
            f"v3.20: 4 个促销词应 reject, reasons={reasons}")


class V320OtherBlacklistsUnchangedTests(unittest.TestCase):
    """v3.20: 其他黑名单仍 enforce (回归保护)."""

    def test_overclaim_3_words_still_rejected(self):
        """3 个 clear overclaim 仍 reject."""
        for kw in _OVERCLAIMS_NO_SUPPORT:
            text = f"这双女鞋是{kw}级别, 让你走在时尚尖端。"
            r = validate_script_quality(text, "时尚女鞋", lang="zh",
                                         target_chars_min=50, target_chars_max=200)
            overclaim_reasons = [x for x in r.get("reasons", []) if "过度承诺" in x]
            self.assertGreater(len(overclaim_reasons), 0,
                f"v3.20 回归: '{kw}' 仍应 reject, reasons={r.get('reasons')}")

    def test_unsupported_attributes_still_rejected_no_detail(self):
        """未填 detail/features 时, 写 面料/材质 仍 reject."""
        # v3.20 修复: 真皮/面料 属于 _UNSUPPORTED_PRODUCT_CLAIMS, 无 detail 时仍 reject
        # 67 字 ≥ 60 (min_chars), 含 女鞋 (on-topic), 不触发跨品类/促销堆叠
        text = ("这双女鞋采用真皮面料, 透气不闷脚, 让你走多远都不觉得累。"
                "轻盈的设计适合日常通勤, 穿上它逛街约会都轻松自在, "
                "百搭款式让你每天都有好心情。")
        r = validate_script_quality(text, "时尚女鞋", lang="zh",
                                     target_chars_min=60, target_chars_max=200)
        attr_reasons = [x for x in r.get("reasons", []) if "包含未提供的具体属性" in x]
        self.assertGreater(len(attr_reasons), 0,
            f"v3.20 回归: 未提供 detail 时 '面料' 应 reject, reasons={r.get('reasons')}")


    def test_cross_category_still_rejected(self):
        """跨品类仍 reject."""
        # v3.20 修复: 衣服 必须出现在「不含 鞋/女鞋」的子句中, 才能触发跨品类 reject
        # 上一版 写 "这双女鞋其实是衣服" 衣服 与 女鞋 同子句, v3.17 算法视为 on-topic 放行
        # 本版 衣服 出现在 "穿上衣服让你在人群中脱颖而出" 子句, 无 鞋 字, 正确 reject
        text = ("这双女鞋很有气质, 穿上衣服让你在人群中脱颖而出。"
                "百搭的款式让你每天都有好心情, 不挑任何风格, "
                "通勤逛街约会都能轻松切换自如。")
        r = validate_script_quality(text, "时尚女鞋", lang="zh",
                                     target_chars_min=60, target_chars_max=200)
        cross_reasons = [x for x in r.get("reasons", []) if "跨品类" in x]
        self.assertGreater(len(cross_reasons), 0,
            f"v3.20 回归: 跨品类 '衣服' 应 reject, reasons={r.get('reasons')}")


class V320Task753ReproduceTests(unittest.TestCase):
    """v3.20: 复现任务 753 类的营销文案, 2 个失败现在能 pass."""

    def test_realistic_fashion_marketing_1(self):
        """典型营销文案 1: 含精挑细选."""
        text = (
            "没有想到女鞋还能这样, 真的是打开新世界了。"
            "春夏季节一双合适的鞋, 能让整个人的状态都松弛自然起来。"
            "百搭的设计不挑任何风格, 通勤逛街约会都能轻松切换。"
            "精挑细选的款式, 让你每一天都充满自信。"
        )
        r = validate_script_quality(text, "时尚女鞋", lang="zh",
                                     target_chars_min=85, target_chars_max=156)
        self.assertTrue(r["valid"],
            f"v3.20: 现实种草文案 1 应 pass, reasons={r.get('reasons')}")

    def test_realistic_fashion_marketing_2(self):
        """典型营销文案 2: 含精英/达人/流行趋势/促销词 (之前踩 2-3 个)."""
        text = (
            "在这个春夏, 让女鞋带你告别单调, 穿上它, 瞬间提升气质!"
            "穿上它, 无论通勤还是周末出游, 都能轻松驾驭, 让你在人群中脱颖而出。"
            "无论是职场精英还是时尚达人, 这款女鞋都是你的最佳选择!"
            "别错过这个机会, 赶紧来试穿吧, 让你成为下一个流行趋势!"
        )
        r = validate_script_quality(text, "时尚女鞋", lang="zh",
                                     target_chars_min=85, target_chars_max=156)
        self.assertTrue(r["valid"],
            f"v3.20: 现实种草文案 2 应 pass, reasons={r.get('reasons')}")

    def test_realistic_fashion_marketing_3(self):
        """典型营销文案 3: 含心头好/惊喜/反复打磨/每一处细节."""
        text = (
            "之前每次换季都头疼, 直到遇到了这款女鞋。"
            "轻盈的鞋面透气不闷脚, 走多远都不觉得累。"
            "经典版型怎么搭配都不会出错, 实用性拉满。"
            "每一双都是我的心头好, 每一次尝试都能带来新的惊喜。"
            "反复打磨的细节, 让你在忙碌的生活中也能享受自在的旅程。"
        )
        r = validate_script_quality(text, "时尚女鞋", lang="zh",
                                     target_chars_min=85, target_chars_max=156)
        self.assertTrue(r["valid"],
            f"v3.20: 现实种草文案 3 应 pass, reasons={r.get('reasons')}")


if __name__ == "__main__":
    unittest.main()
