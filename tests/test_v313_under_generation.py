"""v3.13 守护测试: AI 文案字数总是不足的根因修复."""
import io, re, unittest
from contextlib import redirect_stderr
from unittest.mock import patch
from autokat.core.writer import _build_prompt, generate_script_by_topic_detailed

class T1(unittest.TestCase):
    def test_extend_ideal(self):
        cap = io.StringIO()
        long_enough = "想为日常穿搭多一点灵感, 其实一双合适的鞋就能带来很大的变化。" * 5
        with patch("autokat.core.writer.DEEPSEEK_API_KEY", ""), patch("autokat.core.writer._call_local_model", side_effect=["短", long_enough]), redirect_stderr(cap):
            try: generate_script_by_topic_detailed("时尚女鞋", "种草推荐", target_chars_min=107, target_chars_max=142, max_attempts=2, provider="local")
            except Exception: pass
        ideals = []
        for chunk in cap.getvalue().split("[writer.debug]"):
            if "===== AI PROMPT" in chunk:
                p = chunk.split("===== AI PROMPT", 1)[1].split("===== END PROMPT =====")[0]
                m = re.search(r"理想\s*(\d+)\s*字", p)
                if m: ideals.append(int(m.group(1)))
        self.assertTrue(ideals, "v3.13: 至少 1 个 retry prompt 含 理想 N 字")
        for n in ideals: self.assertEqual(n, (107+142)//2, f"理想 must be (min+max)//2=124, got {n}")

class T2(unittest.TestCase):
    def _p(self): return _build_prompt("时尚女鞋", "种草推荐", lang="zh", target_chars_min=107, target_chars_max=142)
    def test_stop_hint(self): self.assertIn("1-2 句后就结束", self._p())
    def test_min_4_sentences(self): self.assertIn("4 句", self._p())
    def test_short_reject(self): self.assertIn("80 字", self._p())

class T3(unittest.TestCase):
    def _p(self): return _build_prompt("时尚女鞋", "种草推荐", lang="zh", target_chars_min=107, target_chars_max=142)
    def test_mid_total(self):
        quoted = re.findall(r"\"([^\"]{15,80})\"", self._p())
        if len(quoted) >= 4:
            mid_total = sum(len(s) for s in quoted[1:4])
            self.assertGreaterEqual(mid_total, 100, f"MID_VARIANTS 3 句总和 >= 100, got {mid_total}")
    def test_mid_each(self):
        quoted = re.findall(r"\"([^\"]{15,80})\"", self._p())
        if len(quoted) >= 4:
            for i, s in enumerate(quoted[1:4], 1):
                self.assertGreaterEqual(len(s), 30, f"MID 句 {i} 长 {len(s)}")

if __name__ == "__main__": unittest.main()


class E2EQwenStubTests(unittest.TestCase):
    """v3.13 端到端 mock: 模拟 Qwen 0.5B 实际行为 (30字→EXTEND→130字), 验证修复链路."""

    def test_qwen_30_then_extend_passes(self):
        """Qwen 0.5B 第一次输出 14 字 (用户报告的最差情况).
        EXTEND 后模型遵循 v3.13 强提示输出 130+ 字, 应通过 validate.
        """
        import io as _io
        from contextlib import redirect_stderr as _rs
        from autokat.core.writer import generate_script_by_topic_detailed as _gen
        first_output = "想为日常穿搭多一点灵感。"
        second_output = (
            "想为日常穿搭多一点灵感, 其实一双合适的鞋就能带来很大的变化。"
            "春夏季节穿上轻便的款式, 整体造型也跟着松弛自然起来, 走起路来都更有节奏感。"
            "百搭的设计不挑任何风格也不挑任何场合, 通勤逛街约会出游都能轻松切换。"
            "用舒服的步调走出自己的味道, 让每个普通一天都多一点新鲜感, 也有仪式感。"
        )
        cap = _io.StringIO()
        with patch("autokat.core.writer.DEEPSEEK_API_KEY", ""), \
             patch("autokat.core.writer._call_local_model", side_effect=[first_output, second_output]), \
             _rs(cap):
            result = _gen(
                "时尚女鞋", "种草推荐",
                target_chars_min=107, target_chars_max=142, max_attempts=3,
                provider="local",
            )
        self.assertTrue(result["quality"]["valid"],
            f"v3.13: Qwen 14字 + EXTEND 应通过, reasons={result['quality']['reasons']}")
        self.assertIn("百搭", result["text"])
        self.assertGreaterEqual(len(result["text"]), 107,
            f"v3.13: 输出应 >= 107 字, got {len(result['text'])}")

    def test_extend_prompt_has_v313_hints(self):
        """v3.13 验证: retry prompt 必须含新的强提示 (1-2 句停止警告 + 80 字底线 + 理想 124)."""
        import io as _io
        from contextlib import redirect_stderr as _rs
        from autokat.core.writer import generate_script_by_topic_detailed as _gen
        first_output = "想为日常穿搭多一点灵感。"
        long_enough = (
            "想为日常穿搭多一点灵感, 其实一双合适的鞋就能带来很大的变化。"
            "春夏季节穿上轻便的款式, 整体造型也跟着松弛自然起来, 走起路来都更有节奏感。"
            "百搭的设计不挑任何风格也不挑任何场合, 通勤逛街约会出游都能轻松切换。"
            "用舒服的步调走出自己的味道, 让每个普通一天都多一点新鲜感, 也有仪式感。"
        )
        cap = _io.StringIO()
        with patch("autokat.core.writer.DEEPSEEK_API_KEY", ""), \
             patch("autokat.core.writer._call_local_model", side_effect=[first_output, long_enough]), \
             _rs(cap):
            try: _gen("时尚女鞋", "种草推荐", target_chars_min=107, target_chars_max=142, max_attempts=2, provider="local")
            except Exception: pass
        retry_prompts = []
        for chunk in cap.getvalue().split("[writer.debug]"):
            if "EXTEND" in chunk and "===== AI PROMPT" in chunk:
                p = chunk.split("===== AI PROMPT", 1)[1].split("===== END PROMPT =====")[0]
                retry_prompts.append(p)
        self.assertTrue(retry_prompts, "v3.13: 至少应有一次 EXTEND retry prompt")
        p = retry_prompts[0]
        self.assertIn("理想 124 字", p,
            f"v3.13: EXTEND prompt 必须含 '理想 124 字' (旧 bug 算出 60 字让模型进一步缩水). got: {p[:300]}")
        self.assertIn("还差", p,
            "v3.13: EXTEND prompt 必须含 '还差 X 字' (v3.8 既有, 防回归)")
        self.assertIn("不要从头重写", p,
            "v3.13: EXTEND prompt 必须含 '不要从头重写'")
