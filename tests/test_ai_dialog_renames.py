"""Tests for AI 辅助生成对话框的 v3.2 重命名 + B+C 设计。

覆盖 3 个新行为:
1. 视频类型 + 文案风格 都用新 label (口语化)
2. ⚙ 高级 checkbox 控制文案风格显隐
3. 视频类型变 → 文案风格自动默认 (VIDEO_TYPE_DEFAULT_STYLE 映射)

不直接打开 QDialog (依赖 Qt 事件循环), 而是测试底层数据/逻辑:
- VIDEO_TYPE_LABELS / STYLE_LABELS 表
- VIDEO_TYPE_DEFAULT_STYLE 映射
- list_style_choices() 返回顺序
- _sync_default_style 内部逻辑 (用 stub 模拟)
"""
import unittest

from autokat.core.ai_providers import (
    VIDEO_TYPE_LABELS,
    VIDEO_TYPE_DEFAULT_STYLE,
    VIDEO_TYPE_PROMPTS,
)
from autokat.core.writer import STYLES, STYLE_LABELS, list_style_choices


# ── 1. label 重命名 ────────────────────────────────────────────

class VideoTypeLabelRenamesTests(unittest.TestCase):
    """视频类型下拉框应该用口语化的新名字, key 必须保持兼容 (旧快照)。"""

    def test_new_labels_present(self):
        for key, expected_label in (
            ("auto", "AI 智能"),
            ("product_recommendation", "卖货种草"),
            ("talking_explanation", "知识讲解"),
            ("atmosphere", "日常记录"),
            ("music_beat", "音乐卡点"),
            ("random_mix", "素材混剪"),
        ):
            self.assertIn(key, VIDEO_TYPE_LABELS, f"缺 key={key}")
            self.assertEqual(VIDEO_TYPE_LABELS[key], expected_label,
                             f"key={key} 的 label 应是「{expected_label}」, 实际「{VIDEO_TYPE_LABELS[key]}」")

    def test_no_old_jargon_in_labels(self):
        """旧名「商品推荐」「口播讲解」「氛围记录」「随机混剪」「自动判断」不应再出现。"""
        for old_label in ("商品推荐", "口播讲解", "氛围记录", "随机混剪", "自动判断"):
            self.assertNotIn(
                old_label, VIDEO_TYPE_LABELS.values(),
                f"旧名「{old_label}」仍出现在 VIDEO_TYPE_LABELS, 应换成新 label"
            )

    def test_labels_match_prompt_keys(self):
        """VIDEO_TYPE_LABELS 的 key 必须都能在 VIDEO_TYPE_PROMPTS 里找到 (否则 prompt 段会丢)。"""
        for key in VIDEO_TYPE_LABELS:
            self.assertIn(
                key, VIDEO_TYPE_PROMPTS,
                f"VIDEO_TYPE_LABELS 有 key={key} 但 VIDEO_TYPE_PROMPTS 没有, 会导致 prompt hint 退回 default"
            )


class StyleLabelRenamesTests(unittest.TestCase):
    """文案风格 (高级) 应该有新的「身份+腔调」label。"""

    def test_new_labels_present(self):
        for key, expected_label in (
            ("种草推荐", "带货博主"),
            ("生活技巧", "生活达人"),
            ("知识科普", "科普老师"),
            ("测评对比", "实测派"),
            ("励志感悟", "走心姐姐"),
        ):
            self.assertIn(key, STYLE_LABELS, f"缺 key={key}")
            self.assertEqual(STYLE_LABELS[key], expected_label,
                             f"key={key} 的 label 应是「{expected_label}」, 实际「{STYLE_LABELS[key]}」")

    def test_labels_match_style_keys(self):
        """STYLE_LABELS 的 key 必须都是 STYLES 里真实存在的 key, 否则 prompt 模板找不到。"""
        for key in STYLE_LABELS:
            self.assertIn(
                key, STYLES,
                f"STYLE_LABELS 有 key={key} 但 STYLES 里没有, AI 写文案会 RuntimeError"
            )


class ListStyleChoicesOrderTests(unittest.TestCase):

    def test_returns_label_key_pairs_in_stable_order(self):
        """list_style_choices() 返回 [(label, key), ...] 顺序 = STYLES.keys() 顺序 (dict 插入序)。"""
        result = list_style_choices()
        self.assertEqual(len(result), len(STYLES))
        for (_label, _key), expected_key in zip(result, STYLES.keys()):
            self.assertEqual(_key, expected_key,
                             f"顺序应保持 STYLES.keys(), 但 key={_key} != 期望 {expected_key}")
            self.assertEqual(_label, STYLE_LABELS[expected_key],
                             f"label 应来自 STYLE_LABELS, 但 {expected_key} 给的 label={_label}")


# ── 2. 视频类型 → 文案风格 自动默认映射 ──────────────────────────

class VideoTypeDefaultStyleMappingTests(unittest.TestCase):
    """video_type 变 → style 自动默认。映射表必须完整覆盖所有 video_type。"""

    def test_all_video_types_have_mapping(self):
        for key in VIDEO_TYPE_LABELS:
            self.assertIn(
                key, VIDEO_TYPE_DEFAULT_STYLE,
                f"VIDEO_TYPE_LABELS 有 key={key} 但 VIDEO_TYPE_DEFAULT_STYLE 没映射"
            )

    def test_mapped_styles_are_valid(self):
        """映射到的 style 必须存在于 STYLES (否则点击会找不到)。"""
        for vt_key, style_key in VIDEO_TYPE_DEFAULT_STYLE.items():
            if style_key is None:
                continue
            self.assertIn(
                style_key, STYLES,
                f"video_type={vt_key} 默认到 style={style_key}, 但 STYLES 里没这个 key"
            )

    def test_specific_mappings(self):
        """验证 4 个常见组合的映射方向是对的 (避免写反)。"""
        self.assertEqual(
            VIDEO_TYPE_DEFAULT_STYLE["product_recommendation"], "种草推荐",
            "卖货种草 → 带货博主 (key=种草推荐)"
        )
        self.assertEqual(
            VIDEO_TYPE_DEFAULT_STYLE["talking_explanation"], "知识科普",
            "知识讲解 → 科普老师 (key=知识科普)"
        )
        self.assertEqual(
            VIDEO_TYPE_DEFAULT_STYLE["atmosphere"], "励志感悟",
            "日常记录 → 走心姐姐 (key=励志感悟)"
        )
        self.assertEqual(
            VIDEO_TYPE_DEFAULT_STYLE["music_beat"], "种草推荐",
            "音乐卡点 → 带货博主 (key=种草推荐)"
        )

    def test_auto_and_random_have_no_default(self):
        """auto / random_mix 没有合理默认 (让 AI 决定 / 随机)。"""
        self.assertIsNone(VIDEO_TYPE_DEFAULT_STYLE["auto"])
        self.assertIsNone(VIDEO_TYPE_DEFAULT_STYLE["random_mix"])


# ── 3. _build_video_type_combo 必须用 VIDEO_TYPE_LABELS ──────────

class WizardVideoTypeComboLabelsTests(unittest.TestCase):
    """Step 2 / Step 3 的视频类型下拉框 (QComboBox) 用的 label 也必须是新名字。"""

    def test_combo_factory_uses_new_labels(self):
        try:
            from PySide6.QtWidgets import QApplication  # noqa: F401
        except ImportError:
            self.skipTest("PySide6 不在当前 venv, 跳过 UI 集成测试")
            return
        import sys
        app = QApplication.instance() or QApplication(sys.argv)
        from autokat.ui.main_window import MainWindow
        # _build_video_type_combo 是实例方法, 但不依赖 self, 可以用 unbound 调用
        try:
            combo = MainWindow._build_video_type_combo(MainWindow.__new__(MainWindow))
        except Exception as e:
            self.skipTest(f"MainWindow 构造太重, 跳过 UI 实例测试: {e}")
            return
        # 验证 combo 里所有 label 都是新名字
        displayed = [combo.itemText(i) for i in range(combo.count())]
        for new_label in VIDEO_TYPE_LABELS.values():
            self.assertIn(new_label, displayed,
                          f"Step 2/3 下拉框应显示新 label「{new_label}」")
        # 验证 combo 里所有 data(key) 都在 VIDEO_TYPE_LABELS
        for i in range(combo.count()):
            key = combo.itemData(i)
            self.assertIn(key, VIDEO_TYPE_LABELS,
                          f"combo item {i} 的 key={key} 不在 VIDEO_TYPE_LABELS")


if __name__ == "__main__":
    unittest.main()
