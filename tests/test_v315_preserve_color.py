"""v3.15 守护测试: 修 2 个真实渲染问题.

问题 1: 任务 568 第 4/5 条 NameError: name '_log' is not defined
  - autokat/core/renderer.py:render_simple 之前在嵌套函数体内
    调用 _log(), 但 _log 只在 create_and_run_batch 顶层作用域定义, 不可见.
  - 修复: 改用 print() + logging 双写, 不会因为 NameError 让整条渲染挂掉.

问题 2: 用户反馈 "不差异化扰动" 选了但颜色还是变
  - 之前在 ffmpeg output 用 -pix_fmt yuv420p 强制转换, 编码器会做
    PC range (0-255) → TV range (16-235) 隐式色阶转换, 即便没选扰动也偏色.
  - 修复: 把 format=yuv420p 移到 -vf filter chain (不转色阶, 只改 wrapper),
    没选扰动时再加 setparams=...1:1:1:1 把 color_primaries/trc/space/range
    全标 unspecified, 让播放器按源片原始元数据渲染, 严格保色.
"""
import inspect
import unittest


class V315NameErrorFixTests(unittest.TestCase):
    """v3.15 修: 任务 568 NameError (_log not defined) 守护测试."""

    def test_shortfall_log_uses_print_not_log(self):
        """v3.15: shortfall_sec > 2.0 时的 warn log 不应再调 _log (会 NameError).
        改用 print() + logging.getLogger().warning(), 保证渲染流程不被异常中断.
        """
        from autokat.core import renderer
        src = inspect.getsource(renderer.render_simple)
        # 关键判定: 修复后, 整段 _log( 调用不应再出现
        # 只允许 print( / logging.getLogger 出现
        self.assertNotIn("_log(\n                    f\"⚠️ 动态画面", src,
            "v3.15: shortfall warn 仍调 _log, 会 NameError 让渲染挂掉")
        self.assertIn('print(f"[渲染] ', src,
            "v3.15: 应改用 print() 打 warn")
        self.assertIn("logging.getLogger(__name__).warning", src,
            "v3.15: 应同时写 logging (UI / 文件 log)")

    def test_no_log_function_defined_in_render_simple(self):
        """v3.15: render_simple 函数体不应定义 _log 局部函数
        (说明 _log 之前是被错误地假设可见)."""
        from autokat.core import renderer
        src = inspect.getsource(renderer.render_simple)
        self.assertNotIn("def _log(", src,
            "v3.15: render_simple 不应定义 _log 局部函数")


class V315ColorPreservationTests(unittest.TestCase):
    """v3.15 修: 不选扰动时颜色应保持原片, 不做 PC→TV 隐式转换."""

    def test_simple_compose_uses_format_filter_not_pixfmt_flag(self):
        """v3.15: _compose_cached_segments 不应在 output flag 用 -pix_fmt yuv420p
        (会触发 PC→TV 隐式色阶转换). 改用 -vf chain 的 format=yuv420p filter.
        """
        from autokat.core import renderer
        src = inspect.getsource(renderer._compose_cached_segments)
        self.assertNotIn('-pix_fmt", "yuv420p"', src,
            "v3.15: _compose_cached_segments 不应再用 -pix_fmt output flag")

    def test_simple_compose_no_perturbation_adds_setparams_unspecified(self):
        """v3.15: 不选扰动时, filter chain 末尾应加 setparams=...1:1:1:1
        把 color_primaries/trc/space/range 全标 unspecified, 严格保色."""
        from autokat.core import renderer
        src = inspect.getsource(renderer._compose_cached_segments)
        self.assertIn("if not perturbation:", src,
            "v3.15: 应有 'if not perturbation' 分支")
        self.assertIn("setparams=color_primaries=1:color_trc=1:colorspace=1:range=1", src,
            "v3.15: 不选扰动时应加 setparams=...1:1:1:1 保色")
        self.assertIn("format=yuv420p,", src,
            "v3.15: 应有 format=yuv420p filter (不转色阶)")

    def test_main_render_uses_format_filter_not_pixfmt_flag(self):
        """v3.15: render_simple 主渲染路径同样不应有 -pix_fmt yuv420p flag."""
        from autokat.core import renderer
        src = inspect.getsource(renderer.render_simple)
        self.assertNotIn('-pix_fmt", "yuv420p"', src,
            "v3.15: render_simple 不应再用 -pix_fmt output flag")

    def test_main_render_no_perturbation_adds_setparams_unspecified(self):
        """v3.15: 主渲染路径不选扰动时也应加 setparams=...1:1:1:1."""
        from autokat.core import renderer
        src = inspect.getsource(renderer.render_simple)
        self.assertIn("setparams=color_primaries=1:color_trc=1:colorspace=1:range=1", src,
            "v3.15: 主渲染不选扰动时应加 setparams=...1:1:1:1 保色")


if __name__ == "__main__":
    unittest.main()
