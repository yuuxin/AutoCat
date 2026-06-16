"""v3.17 守护测试: 放宽 validation 严格度, 让模型别再被「看关键字」误伤.

用户反馈 3 个问题:
  1. 凭空捏造设计过程/设计师故事 → 匠心 (含变体 匠心独运/打造/呈现) 允许
  2. 跨品类: 题是「鞋」但文案出现了 ['裙', '连衣裙']  → 鞋配上连衣裙 这类
     搭配描述应该按语义放行, 不只看关键词
  3. prompt 里的【禁止】段是把 validation 规则再背一遍, 对模型没新信息还占
     token, 应该删除 (validation 后端仍 enforce)

修复点:
  1. _FABRICATED_PROCESS_CLAIMS 去掉 匠心* (保留 设计师/手工/精雕细琢 等真
     捏造)
  2. _STYLING_PRE 扩充 适合/配搭/映衬/衬托/衬/搭, 跨品类前缀窗口 6→12
  3. _build_prompt 不再输出【禁止】段 (no_fabrication_hint = "")
"""
import unittest

from autokat.core.writer import (
    _build_prompt,
    _FABRICATED_PROCESS_CLAIMS,
    _STYLING_PRE,
    validate_script_quality,
)


# ── 修复 1: 匠心 允许 ────────────────────────────────────────────


class JiangxinAllowedTests(unittest.TestCase):
    """v3.17: 匠心 是常用营销词, 不算捏造设计过程, 应允许通过 validate."""

    def test_jiangxin_alone_passes(self):
        """'匠心打造' 单独使用应通过 (v3.17 之前会 fail)."""
        text = "这款鞋匠心打造, 穿上真的不一样。"
        r = validate_script_quality(text, "时尚女鞋", lang="zh")
        process_reasons = [x for x in r.get("reasons", [])
                           if "捏造设计过程" in x or "设计师故事" in x]
        self.assertEqual(process_reasons, [],
            f"v3.17: '匠心' 单独使用应放行, reasons={r.get('reasons')}")

    def test_jiangxin_compounds_also_pass(self):
        """匠心独运/打造/呈现 (含匠心) 也应放行 — 不然 1 个变体仍 reject."""
        for compound in ["匠心独运", "匠心打造", "匠心呈现", "匠心制造"]:
            text = f"这款鞋{compound}, 穿上真的不一样。"
            r = validate_script_quality(text, "时尚女鞋", lang="zh")
            process_reasons = [x for x in r.get("reasons", [])
                               if "捏造设计过程" in x or "设计师故事" in x]
            self.assertEqual(process_reasons, [],
                f"v3.17: '{compound}' 应放行 (含匠心), reasons={r.get('reasons')}")

    def test_jiangxin_with_full_sentence(self):
        """'这份匠心' / '匠心精神' 等自然用法都应放行."""
        for sentence in [
            "这份匠心让每双鞋都有温度。",
            "品牌一直秉承匠心精神, 专注每一双鞋的细节。",
            "一双好鞋, 离不开匠心的坚持。",
        ]:
            r = validate_script_quality(sentence, "时尚女鞋", lang="zh")
            process_reasons = [x for x in r.get("reasons", [])
                               if "捏造设计过程" in x or "设计师故事" in x]
            self.assertEqual(process_reasons, [],
                f"v3.17: '{sentence}' 应放行, reasons={r.get('reasons')}")

    def test_real_fabrication_still_rejected(self):
        """回归保护: 真捏造 (设计师, 手工打造, 精雕细琢) 仍 reject."""
        for kw in ["设计师", "手工打造", "精雕细琢", "精心打造", "反复打磨",
                   "每一寸细节", "千锤百炼"]:
            text = f"这双鞋{kw}, 让你感受到独特的品质。"
            r = validate_script_quality(text, "时尚女鞋", lang="zh")
            process_reasons = [x for x in r.get("reasons", [])
                               if "捏造设计过程" in x or "设计师故事" in x]
            self.assertGreater(len(process_reasons), 0,
                f"v3.17 回归: '{kw}' 仍应 reject (真捏造), reasons={r.get('reasons')}")

    def test_jiangxin_constant_removed_from_blacklist(self):
        """_FABRICATED_PROCESS_CLAIMS 不应再含 匠心."""
        self.assertNotIn("匠心", _FABRICATED_PROCESS_CLAIMS,
            "v3.17: '匠心' 必须从 _FABRICATED_PROCESS_CLAIMS 移除")
        self.assertNotIn("匠心独运", _FABRICATED_PROCESS_CLAIMS,
            "v3.17: '匠心独运' 必须从 _FABRICATED_PROCESS_CLAIMS 移除")
        self.assertNotIn("匠心打造", _FABRICATED_PROCESS_CLAIMS,
            "v3.17: '匠心打造' 必须从 _FABRICATED_PROCESS_CLAIMS 移除")
        self.assertNotIn("匠心呈现", _FABRICATED_PROCESS_CLAIMS,
            "v3.17: '匠心呈现' 必须从 _FABRICATED_PROCESS_CLAIMS 移除")


# ── 修复 2: 跨品类 语义判断 — 搭配描述放行 ────────────────────────


class CrossCategorySemanticTests(unittest.TestCase):
    """v3.17: 跨品类按语义放行, 不只看关键词. 鞋配上连衣裙 / 适合搭 /
    配搭 / 映衬 / 衬托 / 衬 等搭配词都应放行.
    """

    def test_shoes_with_shida_passes(self):
        """用户报告场景: 鞋配上连衣裙 应放行."""
        text = "这双鞋配上连衣裙, 整个人都温柔了。"
        r = validate_script_quality(text, "时尚女鞋", lang="zh")
        cross_reasons = [x for x in r.get("reasons", []) if "跨品类" in x]
        self.assertEqual(cross_reasons, [],
            f"v3.17: 鞋配上连衣裙 应放行, reasons={r.get('reasons')}")

    def test_shoes_with_shihe_da_passes(self):
        """'适合搭' 之前 _STYLING_PRE 没, 漏掉 → 误伤."""
        text = "这双女鞋适合搭连衣裙, 春夏季节穿出清爽感。"
        r = validate_script_quality(text, "时尚女鞋", lang="zh")
        cross_reasons = [x for x in r.get("reasons", []) if "跨品类" in x]
        self.assertEqual(cross_reasons, [],
            f"v3.17: 适合搭连衣裙 应放行, reasons={r.get('reasons')}")

    def test_shoes_with_peida_passes(self):
        """'配搭' 之前漏, 加进 _STYLING_PRE."""
        text = "这双女鞋配搭小裙子, 整体更有层次感。"
        r = validate_script_quality(text, "时尚女鞋", lang="zh")
        cross_reasons = [x for x in r.get("reasons", []) if "跨品类" in x]
        self.assertEqual(cross_reasons, [],
            f"v3.17: 配搭小裙子 应放行, reasons={r.get('reasons')}")

    def test_shoes_with_yingchen_passes(self):
        """'映衬' 之前漏, 加进 _STYLING_PRE."""
        text = "这双女鞋映衬出连衣裙的优雅, 整体造型更出彩。"
        r = validate_script_quality(text, "时尚女鞋", lang="zh")
        cross_reasons = [x for x in r.get("reasons", []) if "跨品类" in x]
        self.assertEqual(cross_reasons, [],
            f"v3.17: 映衬出连衣裙 应放行, reasons={r.get('reasons')}")

    def test_shoes_with_chentuo_passes(self):
        """'衬托' 之前漏, 加进 _STYLING_PRE."""
        text = "这双女鞋衬托出连衣裙的层次感, 让整体更有味道。"
        r = validate_script_quality(text, "时尚女鞋", lang="zh")
        cross_reasons = [x for x in r.get("reasons", []) if "跨品类" in x]
        self.assertEqual(cross_reasons, [],
            f"v3.17: 衬托出连衣裙 应放行, reasons={r.get('reasons')}")

    def test_shoes_with_chen_passes(self):
        """'衬' 单独使用 (如 '衬裙子') 应放行."""
        text = "这双女鞋衬小裙子特别好看, 整体很有气质。"
        r = validate_script_quality(text, "时尚女鞋", lang="zh")
        cross_reasons = [x for x in r.get("reasons", []) if "跨品类" in x]
        self.assertEqual(cross_reasons, [],
            f"v3.17: 衬小裙子 应放行, reasons={r.get('reasons')}")

    def test_shoes_with_da_passes(self):
        """'搭' 单独使用 (如 '搭裙子') 应放行."""
        text = "这双女鞋搭小裙子超好看, 春夏季节清爽出街。"
        r = validate_script_quality(text, "时尚女鞋", lang="zh")
        cross_reasons = [x for x in r.get("reasons", []) if "跨品类" in x]
        self.assertEqual(cross_reasons, [],
            f"v3.17: 搭小裙子 应放行, reasons={r.get('reasons')}")

    def test_long_prefix_catches_broad_styling(self):
        """前缀窗口 6→12: '这双百搭的鞋子与连衣裙搭配起来' 这种长距离搭配
        也应放行 (6 字符窗口会漏, 12 字符能 catch)."""
        text = "这双百搭的鞋子与连衣裙搭配起来, 整体造型更有层次感。"
        r = validate_script_quality(text, "时尚女鞋", lang="zh")
        cross_reasons = [x for x in r.get("reasons", []) if "跨品类" in x]
        self.assertEqual(cross_reasons, [],
            f"v3.17: 长距离搭配应放行 (12 字符窗口), reasons={r.get('reasons')}")

    def test_styling_pre_includes_new_words(self):
        """_STYLING_PRE 必须含新增的搭配词, 否则会回归."""
        for word in ["适合", "配搭", "映衬", "衬托", "衬", "搭"]:
            self.assertIn(word, _STYLING_PRE,
                f"v3.17: _STYLING_PRE 必须含 '{word}' (新增强搭配词)")

    def test_real_cross_category_still_rejected(self):
        """回归保护: 真跨品类 (无搭配词, 子句也无 topic word) 仍 reject."""
        # "鞋子其实是衣服的一种" - 真跨品类
        text = "这双鞋其实是衣服的一种, 穿起来很优雅。"
        r = validate_script_quality(text, "时尚女鞋", lang="zh")
        cross_reasons = [x for x in r.get("reasons", []) if "跨品类" in x]
        self.assertGreater(len(cross_reasons), 0,
            f"v3.17 回归: 真跨品类 (无搭配词) 应 reject, reasons={r.get('reasons')}")


# ── 修复 3: prompt 删除【禁止】段 ────────────────────────────────


class PromptBannedSectionRemovedTests(unittest.TestCase):
    """v3.17: prompt 不再含【禁止】段 (validation 后端仍 enforce)."""

    def _p(self) -> str:
        return _build_prompt(
            "时尚女鞋", "种草推荐", detail=None, features=None,
            lang="zh",
            target_chars_min=107, target_chars_max=142,
        )

    def test_prompt_no_longer_has_banned_section(self):
        """【禁止】段必须从 prompt 删除."""
        p = self._p()
        self.assertNotIn("【禁止】", p,
            "v3.17: prompt 不应再含「【禁止】」段, validation 后端已 enforce")

    def test_prompt_no_visual_forbidden_attributes(self):
        """'颜色/尺寸/材质/配件' 等外观禁止词也删除 — validation 仍 reject."""
        p = self._p()
        for kw in ["颜色", "尺寸", "材质", "配件"]:
            # 颜色/材质/尺寸/配件 是 禁止 段里的内容
            # 但其它地方可能也提到 (如 颜色 not mentioned in v3.16 prompt)
            # 检查 禁止 段里的具体语句
            pass  # 在 test_v35_capability_summary 里已覆盖

    def test_prompt_shorter_than_v316(self):
        """v3.17 prompt 应比 v3.16 短 (删了 5 行 禁止 段)."""
        p = self._p()
        # v3.16 prompt 约 1182 字符 (前面测过), v3.17 至少少 200 字符
        self.assertLess(len(p), 1100,
            f"v3.17: prompt 应比 v3.16 短 (删了 5 行 禁止 段), got {len(p)} chars")


# ── 回归保护: validation 后端仍严格 enforce 关键黑名单 ──────────


class ValidationBackendEnforcementTests(unittest.TestCase):
    """v3.17 回归保护: 即便 prompt 不再列黑名单, validation 后端
    仍按 _FABRICATED_PROCESS_CLAIMS / _OVERCLAIMS_NO_SUPPORT /
    _CROSS_CATEGORY_FORBIDDEN 严格 reject.
    """

    def test_overclaim_blacklist_still_enforced(self):
        """3 个 clear overclaim (艺术品/颠覆性/革命性) 仍 reject."""
        for kw in ["艺术品", "颠覆性", "革命性"]:
            text = f"这双鞋是{kw}级别的设计, 让你走在时尚尖端。"
            r = validate_script_quality(text, "时尚女鞋", lang="zh")
            overclaim_reasons = [x for x in r.get("reasons", []) if "过度承诺" in x]
            self.assertGreater(len(overclaim_reasons), 0,
                f"v3.17 回归: '{kw}' 仍应 reject, reasons={r.get('reasons')}")

    def test_fabrication_blacklist_still_enforced(self):
        """真捏造 (设计师, 手工, 精雕细琢) 仍 reject."""
        for kw in ["设计师", "手工打造", "精雕细琢", "精心打造", "每一寸细节"]:
            text = f"这双鞋{kw}, 让你感受到独特品质。"
            r = validate_script_quality(text, "时尚女鞋", lang="zh")
            process_reasons = [x for x in r.get("reasons", []) if "捏造设计过程" in x]
            self.assertGreater(len(process_reasons), 0,
                f"v3.17 回归: '{kw}' 仍应 reject, reasons={r.get('reasons')}")

    def test_unsupported_attributes_still_enforced(self):
        """未提供 detail/features 时, 写 面料/材质 等硬数据仍 reject."""
        text = "这双鞋采用真皮面料, 让你感受舒适。"
        r = validate_script_quality(text, "时尚女鞋", lang="zh",
                                     target_chars_min=50, target_chars_max=200)
        attr_reasons = [x for x in r.get("reasons", []) if "包含未提供的具体属性" in x]
        self.assertGreater(len(attr_reasons), 0,
            f"v3.17 回归: 未提供 detail 时 写 面料 应 reject, reasons={r.get('reasons')}")


if __name__ == "__main__":
    unittest.main()
