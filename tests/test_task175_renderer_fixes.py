"""Regression tests for renderer.py fixes related to task 175 issues 2 + 4.

Issue 2: 没选差异化扰动但成片有色彩变化
   → 期望: render_simple 不再硬编码 -colorspace bt709 -color_primaries bt709
            -color_trc bt709 -color_range tv，避免源片若为 bt601 被 ffmpeg 隐性转色。
   → 期望: _batch_color_holder 同样清空。

Issue 4: 生成视频 03 时报 RuntimeError 缺帧
   → 期望: dynamic shortfall (actual_video_frames < target_frames - 1) 改为
            warn + log，不 raise；让 tpad=stop_mode=clone 在最终合成阶段补帧。
"""
import re
import unittest


RENDERER_PATH = "autokat/core/renderer.py"


def _read_renderer_source() -> str:
    with open(RENDERER_PATH, encoding="utf-8") as f:
        return f.read()


# ── Issue 2: 去掉硬编码 bt709 色彩标签 ───────────────────────

class NoHardcodedBt709ColorTagsTests(unittest.TestCase):
    def test_render_simple_no_longer_writes_bt709_tags(self):
        src = _read_renderer_source()
        # 找 render_simple 函数的定义起点，然后取它到下一个顶级 def 之间的内容
        m = re.search(r"^def render_simple\b", src, re.MULTILINE)
        self.assertIsNotNone(m, "render_simple 函数未找到")
        start = m.start()
        # 从 start 往后找下一个 ^def 或 ^class
        rest = src[start + 1:]
        nxt = re.search(r"^def \w+|^class \w+", rest, re.MULTILINE)
        body = src[start:start + 1 + (nxt.start() if nxt else len(rest))]
        # 兜底：若没找到下一个 def，至少取到文件尾
        # 这 4 个 tag 不应在 render_simple 内出现
        for tag in ('-colorspace "bt709"', '-color_primaries "bt709"',
                    '-color_trc "bt709"', '-color_range "tv"'):
            self.assertNotIn(tag, body,
                             f"render_simple 仍硬编码 {tag}，会触发源片隐式转色")

    def test_batch_color_holder_is_empty(self):
        src = _read_renderer_source()
        m = re.search(
            r"_batch_color_holder\s*=\s*(\[[^\]]*\])",
            src, re.DOTALL,
        )
        self.assertIsNotNone(m, "_batch_color_holder 列表未找到")
        self.assertEqual(m.group(1).strip(), "[]",
                         "_batch_color_holder 应为空（不污染同 batch 输出的色空间）")

    def test_pix_fmt_yuv420p_still_present(self):
        # 兼容性需要 -pix_fmt yuv420p (QuickTime / 老硬件支持)
        src = _read_renderer_source()
        self.assertIn("-pix_fmt", src)
        self.assertIn("yuv420p", src)


# ── Issue 4: 动态画面短缺改为 warn + log ─────────────────────

class DynamicShortfallWarningTests(unittest.TestCase):
    def test_dynamic_shortfall_no_longer_raises(self):
        src = _read_renderer_source()
        # 找包含 "动态画面" 的 raise RuntimeError 段
        bad_pattern = re.search(
            r"if actual_video_frames < target_frames - 1:\s*\n\s*raise RuntimeError",
            src,
        )
        self.assertIsNone(
            bad_pattern,
            "actual_video_frames < target_frames - 1 仍 raise RuntimeError；"
            "应改为 warn + log，让 tpad=clone 链路补帧",
        )

    def test_dynamic_shortfall_logs_warning(self):
        src = _read_renderer_source()
        m = re.search(
            r"if actual_video_frames < target_frames - 1:(.*?)(?=\n        if|\n        #|\n    [a-z])",
            src, re.DOTALL,
        )
        self.assertIsNotNone(m, "未找到 actual_video_frames < target_frames - 1 分支")
        body = m.group(1)
        # 分支里应包含 _log 调用
        self.assertIn("_log(", body,
                       "shortfall 分支应包含 _log() 记录警告")
        # 应提及 "短于目标" 或类似关键词
        self.assertTrue(
            "短于目标" in body or "shortfall" in body,
            f"shortfall 日志应提及'短于目标'，实际: {body[:200]!r}",
        )

    def test_tpad_still_in_final_vf_chain(self):
        # tpad=stop_mode=clone 必须在最终 -vf 链里，shortfall 时用它补帧
        src = _read_renderer_source()
        self.assertIn("tpad=stop_mode=clone", src,
                       "tpad=stop_mode=clone 不在 ffmpeg 链里，无法在 shortfall 时补帧")


if __name__ == "__main__":
    unittest.main()
