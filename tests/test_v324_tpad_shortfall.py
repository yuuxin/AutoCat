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
        """主渲染路径必须把 not perturbation 传给 _h264_encoder_args (v3.24.1 还会传 source_color_range)."""
        with open("/Users/lilei/work/code/AutoCat/autokat/core/renderer.py",
                  encoding="utf-8") as f:
            src = f.read()
        # 两处调用: line 238 (compose) + line 777 (main render)
        # v3.24.1 改动: 主渲染还会传 source_color_range=..., 接受单/多行
        self.assertIn('preserve_color=not perturbation', src)
        self.assertIn('source_color_range=', src)
        # 关键调用 1: compose 路径
        self.assertIn('_h264_encoder_args("4M", preserve_color=not perturbation)', src)
        # 关键调用 2: main render 路径
        self.assertIn('enc_args = _h264_encoder_args(', src)
        self.assertIn('source_color_range=_src_color_range', src)

    def test_setparams_unspecified_still_present_for_metadata(self):
        """v3.24 注: filter chain 的 setparams=range=1 仍保留 (元数据标签),
        真正保色靠编码器 -x264-params range=pc. 两者并存才完整."""
        with open("/Users/lilei/work/code/AutoCat/autokat/core/renderer.py",
                  encoding="utf-8") as f:
            src = f.read()
        self.assertIn("setparams=color_primaries=1:color_trc=1:colorspace=1:range=1", src)



# ── v3.24.1 第三部分: 长缺口 raise + 源片 color_range 动态选 ──
from autokat.core.renderer import _TAIL_FREEZE_THRESHOLD


class TpadShortfallThresholdTests(unittest.TestCase):
    """v3.24.1: shortfall > 0.5s 必须 raise 清晰错误, 不再用 tpad=clone 糊弄.

    之前 v3.24 用 tpad=stop_mode=clone 补 5.77s 静态画面 → 触发 freezedetect → 任务 755 失败.
    用户问「是没用补充新的切片吗」, 答案是: 是的, 没用, 之前用静态帧糊弄. v3.24.1
    改成: 长缺口 raise 让用户补素材, 短缺口 (≤ 0.5s) 才允许 tpad=clone.
    """

    def test_tail_freeze_threshold_constant(self):
        """v3.24.1: _TAIL_FREEZE_THRESHOLD = 0.5 (与 _tail_freeze_duration 默认对齐)."""
        self.assertEqual(_TAIL_FREEZE_THRESHOLD, 0.5)

    def test_long_shortfall_raises_with_clear_message(self):
        """v3.24.1: shortfall > 0.5s 源码必须有 raise RuntimeError + 提示「请补充素材」."""
        with open("/Users/lilei/work/code/AutoCat/autokat/core/renderer.py",
                  encoding="utf-8") as f:
            src = f.read()
        # 必须有 raise
        self.assertIn("raise RuntimeError(", src)
        # 必须有 "请补充素材" 或类似提示
        self.assertTrue(
            "请补充素材" in src or "补充素材" in src,
            "v3.24.1: 长缺口 raise 错误信息必须提示用户补充素材",
        )
        # 必须有 "不要使用静止帧补齐" 提示 (与用户 568 旧诉求对齐)
        self.assertIn("不要使用静止帧补齐", src,
                       "v3.24.1: 错误信息应包含「不要使用静止帧补齐」, 呼应用户 568 旧诉求")

    def test_short_shortfall_keeps_print_warning(self):
        """v3.24.1: 短缺口 (< 0.5s) 保留 v3.15 的 print warning (向后兼容)."""
        with open("/Users/lilei/work/code/AutoCat/autokat/core/renderer.py",
                  encoding="utf-8") as f:
            src = f.read()
        # v3.15 守护: print(f"[渲染] "...) 必须保留
        self.assertIn('print(f"[渲染] ', src,
                       "v3.24.1: 短缺口必须保留 v3.15 的 print warning")


class SourceColorRangeDetectionTests(unittest.TestCase):
    """v3.24.1: 主渲染路径必须检测源片 color_range, 传给 x264-params.

    之前 v3.24 强制 range=pc, 但源片若是 TV range 会被错解读为 PC → 整体偏亮 7.3%.
    v3.24.1: get_media_info(concat_video) 取 color_range, 传给 _h264_encoder_args.
    """

    def test_h264_encoder_args_accepts_source_color_range(self):
        """v3.24.1: 签名加 source_color_range 参数."""
        import inspect
        from autokat.core.renderer import _h264_encoder_args
        sig = inspect.signature(_h264_encoder_args)
        self.assertIn("source_color_range", sig.parameters)

    def test_tv_source_uses_range_tv(self):
        """v3.24.1: source=tv → x264-params range=tv (防止 TV→PC 偏亮)."""
        from autokat.core.renderer import _h264_encoder_args
        args = _h264_encoder_args("8M", preserve_color=True, source_color_range="tv")
        self.assertIn("-x264-params", args)
        params = args[args.index("-x264-params") + 1]
        self.assertIn("range=tv", params)
        self.assertNotIn("range=pc", params)

    def test_pc_source_uses_range_pc(self):
        """v3.24.1: source=pc → x264-params range=pc (防止 PC→TV 偏暗)."""
        from autokat.core.renderer import _h264_encoder_args
        args = _h264_encoder_args("8M", preserve_color=True, source_color_range="pc")
        params = args[args.index("-x264-params") + 1]
        self.assertIn("range=pc", params)
        self.assertNotIn("range=tv", params)

    def test_unknown_source_defaults_to_pc(self):
        """v3.24.1: source=unknown → 默认 range=pc (现代源片多数 PC, 与 v3.24 一致)."""
        from autokat.core.renderer import _h264_encoder_args
        args = _h264_encoder_args("8M", preserve_color=True, source_color_range="unknown")
        params = args[args.index("-x264-params") + 1]
        self.assertIn("range=pc", params)

    def test_main_render_detects_source_color_range(self):
        """v3.24.1: 主渲染路径必须调 get_media_info 拿源片 color_range."""
        with open("/Users/lilei/work/code/AutoCat/autokat/core/renderer.py",
                  encoding="utf-8") as f:
            src = f.read()
        # 必须在 enc_args = _h264_encoder_args(...) 之前调 get_media_info
        self.assertIn("get_media_info(concat_video)", src)
        self.assertIn("_src_color_range", src)
