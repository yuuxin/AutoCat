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
