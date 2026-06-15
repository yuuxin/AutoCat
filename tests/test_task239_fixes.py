"""Regression tests for task 239 暴露的 3 个问题。

Issue 1: [14:16:24] ❌  [4/5] 第 4 条失败  · 渲染异常:
        NameError: name '_log' is not defined
Issue 2: 差异化扰动选「不扰动」成片色调还是被改了 (变亮)
Issue 3: 第三步中的视频类型下拉框去掉不展示 (见 test_video_type_ui_wiring.py)
"""
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class Task239Issue1LogTypoTests(unittest.TestCase):
    """Issue 1: renderer.py:1226 之前是 _log (未定义) 应是 _log_emit"""

    def test_renderer_imports_log_emit(self):
        """renderer 模块必须 import _log_emit, 因为 _log 没定义"""
        import autokat.core.renderer as r
        self.assertTrue(hasattr(r, "_log_emit"),
                        "renderer 必须 import _log_emit (旧名 _log 没定义)")

    def test_render_task_uses_log_emit_not_bare_log(self):
        """任务 239 bug: _render_task 里调用 _log(...) 报 NameError,
        因为 _log 是 render_simple 内的 closure (不在 _render_task 作用域)。
        必须改成 _log_emit。"""
        import inspect
        from autokat.core import renderer as r
        src = inspect.getsource(r._render_task)
        self.assertIn(
            '_log_emit(f"   🧠 自适应并发:', src,
            "_render_task 里自适应并发的日志必须用 _log_emit, 不能用 _log "
            "(后者是 render_simple 的 closure, 在 _render_task 作用域不存在)",
        )
        self.assertNotIn(
            '_log(f"   🧠 自适应并发:', src,
            "_render_task 里仍用 bare _log(...), 会复现任务 239 的 NameError",
        )


class Task239Issue2Bt709TagTests(unittest.TestCase):
    """Issue 2: 选「不扰动」成片色调还是被改了 (变亮)。

    根因: cached_segment 命令无条件加 bt709 tag (源片若标的是 bt601 会被
    ffmpeg 隐性转色)。
    修复: 改成条件式 — 只有 HDR→SDR tonemap 路径才加 bt709 tag
    (输出实际是 bt709, 加 tag 正确), SDR 源绝对不加。"""

    def test_hdr_cache_command_still_writes_bt709(self):
        """HDR 源 (color_primaries=bt2020) 缓存时仍应加 bt709 tag,
        因为 zscale=...tonemap=mobius... 后输出确实是 bt709。"""
        from autokat.core import slice_cache
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "hdr.mov"
            source.write_bytes(b"x")
            hdr_meta = {
                "color_primaries": "bt2020", "color_space": "bt2020nc",
                "color_transfer": "arib-std-b67",
            }
            with patch.object(slice_cache, "_source_color_metadata",
                              return_value=hdr_meta), \
                 patch.object(slice_cache, "run_ffmpeg", return_value=None) as mock_run, \
                 patch.object(slice_cache.SliceCache, "build",
                              side_effect=lambda k, b: (b(Path(tmp) / "out.mp4"), k)):
                slice_cache.cached_segment(
                    {"source_path": str(source), "duration_frames": 60},
                    fps=30, render_frames=60,
                )
            cmd = mock_run.call_args.args[0]  # 必须在 with 内捕获, mock 退出会还原
            joined = " ".join(str(x) for x in cmd)
            self.assertIn("tonemap=mobius", joined,
                          "HDR 源必须走 zscale+tonemap 路径")
            self.assertIn("-colorspace", joined,
                          "HDR→SDR 后输出确实是 bt709, 必须加 -colorspace bt709 tag")
            # 完整 bt709 套件
            for tag in ("-color_primaries", "-color_trc"):
                self.assertIn(tag, joined,
                              f"HDR 源缓存必须加 {tag} tag (匹配 bt709 输出)")

    def test_sdr_cache_command_no_bt709_tag(self):
        """SDR 源 (color_transfer=bt709 等非 HDR) 不应被强行写 bt709 tag。
        这是任务 239 用户报告的根因: 选「不扰动」但 ffmpeg 看到源片 bt601 被
        隐性转色, 成片变亮。"""
        from autokat.core import slice_cache
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "sdr.mp4"
            source.write_bytes(b"x")
            sdr_meta = {
                "color_primaries": "bt709", "color_space": "bt709",
                "color_transfer": "bt709",
            }
            with patch.object(slice_cache, "_source_color_metadata",
                              return_value=sdr_meta), \
                 patch.object(slice_cache, "run_ffmpeg", return_value=None) as mock_run, \
                 patch.object(slice_cache.SliceCache, "build",
                              side_effect=lambda k, b: (b(Path(tmp) / "out.mp4"), k)):
                slice_cache.cached_segment(
                    {"source_path": str(source), "duration_frames": 60},
                    fps=30, render_frames=60,
                )
            cmd = mock_run.call_args.args[0]  # 必须在 with 内捕获
            joined = " ".join(str(x) for x in cmd).lower()
            for forbidden in ("-colorspace", "-color_primaries", "-color_trc", "-color_range"):
                self.assertNotIn(
                    forbidden, joined,
                    f"SDR 源不应被强行加 {forbidden} tag (任务 239 用户报告 bug 根因)。"
                    f"只有 HDR→SDR tonemap 后输出才是 bt709, 才需要这个 tag。",
                )


class Task239Issue2RendererBt709GuardTests(unittest.TestCase):
    """renderer.py 也没硬塞 bt709 tag (防御性, 跟 slice_cache 一致)"""

    def test_renderer_no_bt709_string_in_cmd_construction(self):
        """renderer.py 不应再硬编码 -colorspace bt709 / -color_range tv 等 tag。
        任务 175 已修过 _compose_cached_segments 和最终输出,
        这条测试守护未来不会再有人加回去。"""
        import inspect
        from autokat.core import renderer as r
        src = inspect.getsource(r)
        for forbidden in ('"bt709"', "'bt709'", '"tv"', "'tv'"):
            self.assertNotIn(
                forbidden, src,
                f"renderer 源码里不应再硬编码 {forbidden} 色彩 tag",
            )


if __name__ == "__main__":
    unittest.main()
