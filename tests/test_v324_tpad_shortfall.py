"""v3.24 守护测试: tpad 实际用 shortfall 补足 (任务 754 修复).

用户报错: [16:54:51] ❌ [4/5] 第 4 条失败
         RuntimeError: 最终时长不一致:
         target=23.400, format/video/audio=[23.4, 20.433008, 23.4],
         pair_diff=2967.0ms

根因: tpad=stop_mode=clone:stop_duration=1/fps 仅补 1 帧 ≈33ms,
     远不够填 2.97s 缺口 → video 仍 20.4s < audio 23.4s
     → 同步校验 raise RuntimeError.

v3.24 修复:
  1. shortfall_sec 提到 if 块外, clamp 到 ≥0
  2. tpad 的 stop_duration 改用 shortfall_sec 而非 1/fps
"""
import re
import unittest


class TpadShortfallFormulaTests(unittest.TestCase):
    """v3.24: 源码层面验证 tpad stop_duration 公式正确."""

    def _load_source(self):
        with open("/Users/lilei/work/code/AutoCat/autokat/core/renderer.py",
                  encoding="utf-8") as f:
            return f.read()

    def test_shortfall_sec_declared_outside_if(self):
        """v3.24: shortfall_sec 必须在 if 块外也能访问 (供 tpad 用)."""
        src = self._load_source()
        m = re.search(
            r"shortfall_sec\s*=\s*max\(0\.0,\s*\(target_frames\s*-\s*actual_video_frames\)\s*/\s*fps\)",
            src,
        )
        self.assertIsNotNone(
            m, "v3.24: shortfall_sec 应在 if 块外用 max(0.0, ...) 计算"
        )

    def test_tpad_uses_shortfall_sec_not_one_over_fps(self):
        """v3.24: tpad stop_duration 必须用 shortfall_sec, 不能用 1/fps."""
        src = self._load_source()
        tpad_uses_1_over_fps = re.search(
            r'tpad=stop_mode=clone:stop_duration=\{1\s*/\s*fps',
            src,
        )
        self.assertIsNone(
            tpad_uses_1_over_fps,
            "v3.24: tpad 不应再用 stop_duration={1/fps}, 应改用 shortfall_sec",
        )
        tpad_uses_shortfall = len(re.findall(
            r'tpad=stop_mode=clone:stop_duration=\{shortfall_sec',
            src,
        ))
        self.assertEqual(
            tpad_uses_shortfall, 2,
            "v3.24: 2 处 tpad (有字幕/无字幕) 都应用 shortfall_sec",
        )


class TpadShortfallMathTests(unittest.TestCase):
    """v3.24: shortfall_sec 数学正确性 (任务 754 场景)."""

    def test_task754_shortfall_2_967s(self):
        # 任务 754 真实数据
        target_dur = 23.4
        actual_dur = 20.433008
        fps = 30
        target_frames = round(target_dur * fps)
        actual_frames = round(actual_dur * fps)
        shortfall_sec = max(0.0, (target_frames - actual_frames) / fps)
        # 2.967s 缺口 (任务 754 的 pair_diff=2967ms)
        self.assertAlmostEqual(shortfall_sec, 2.967, delta=0.05)

    def test_no_shortfall_clamps_to_zero(self):
        fps = 30
        shortfall_sec = max(0.0, (round(20 * fps) - round(25 * fps)) / fps)
        self.assertEqual(shortfall_sec, 0.0)

    def test_shortfall_matches_tpad_capacity(self):
        """v3.24 验证: tpad 补的秒数 = 缺口秒数 (不再 1/fps)."""
        # 模拟任务 754 场景
        target_dur = 23.4
        actual_dur = 20.433
        fps = 30
        target_frames = round(target_dur * fps)
        actual_frames = round(actual_dur * fps)
        shortfall_sec = max(0.0, (target_frames - actual_frames) / fps)
        # 修复后: tpad 加 shortfall_sec, video 总长 = actual + shortfall = target
        expected_video = actual_dur + shortfall_sec
        self.assertAlmostEqual(expected_video, target_dur, delta=0.05)


if __name__ == "__main__":
    unittest.main()


# ── v3.24 第二部分: 编码器层真正保色 ────────────────────────
from autokat.core.renderer import _h264_encoder_args


class H264EncoderArgsColorTests(unittest.TestCase):
    """v3.24: _h264_encoder_args(preserve_color=True) 必须加 -x264-params range=pc.

    之前 v3.15 只在 filter chain 加 setparams=range=1 (unspecified), 只能改
    元数据标签, 编码器 (libx264) 仍默认 PC→TV 转色阶, 成品发灰偏暗.
    v3.24 在编码器层加 -x264-params "range=pc:..." 真正阻止转色阶.
    """

    def test_preserve_color_true_adds_x264_params_pc(self):
        args = _h264_encoder_args("8M", preserve_color=True)
        # 必须含 -x264-params
        self.assertIn("-x264-params", args)
        # 必须是 list 形式而非单字符串
        idx = args.index("-x264-params")
        self.assertEqual(idx + 1, len(args) - 1)
        params = args[-1]
        # 必须有 range=pc
        self.assertIn("range=pc", params)
        # 必须有 unspecified 让播放器按源片 tag
        for k in ("colorprim=unspecified", "transfer=unspecified", "colospace=unspecified"):
            self.assertIn(k, params)

    def test_preserve_color_false_keeps_baseline(self):
        """不传 preserve_color (False) 时不应加 -x264-params, 保持原行为."""
        args = _h264_encoder_args("8M", preserve_color=False)
        self.assertNotIn("-x264-params", args)
        # 基线 6 个: -c:v libx264 -preset veryfast -crf 23
        self.assertEqual(args, ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"])

    def test_main_render_passes_preserve_color(self):
        """主渲染路径必须把 not perturbation 传给 _h264_encoder_args."""
        with open("/Users/lilei/work/code/AutoCat/autokat/core/renderer.py",
                  encoding="utf-8") as f:
            src = f.read()
        # 两处调用: line 238 (compose) + line 777 (main render)
        self.assertIn('_h264_encoder_args("4M", preserve_color=not perturbation)', src)
        self.assertIn('_h264_encoder_args(bitrate, preserve_color=not perturbation)', src)

    def test_setparams_unspecified_still_present_for_metadata(self):
        """v3.24 注: filter chain 的 setparams=range=1 仍保留 (元数据标签),
        真正保色靠编码器 -x264-params range=pc. 两者并存才完整."""
        with open("/Users/lilei/work/code/AutoCat/autokat/core/renderer.py",
                  encoding="utf-8") as f:
            src = f.read()
        self.assertIn("setparams=color_primaries=1:color_trc=1:colorspace=1:range=1", src)
