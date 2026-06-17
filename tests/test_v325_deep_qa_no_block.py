"""v3.25 守护测试: 深度 ASR/OCR 验收失败不阻塞任务 done 状态.

用户报错 (任务 756): 4 个 mp4 成品都在 output/, 但任务标 failed.
根因: renderer.py:1358 deep 验收失败时把 final_status 覆盖为 "failed"
并调 update_task_status(task_id, "failed"), 误导用户任务失败.
正确语义: 渲染成功=mp4 存在=任务 done, 深度验收是内容质量维度仅警告.
"""
import re
import unittest


class DeepQANoBlockTests(unittest.TestCase):
    """v3.25: deep 验收失败不阻塞任务 done 状态, 仅 log 警告."""

    def _load(self):
        with open("/Users/lilei/work/code/AutoCat/autokat/core/renderer.py",
                  encoding="utf-8") as f:
            return f.read()

    def test_deep_fail_does_not_set_final_status_failed(self):
        """v3.25: 源码不能再有 'elif not deep_result.get(.passed.): final_status = .failed.'"""
        src = self._load()
        bad = re.search(
            r'elif not deep_result\.get\(["\']passed["\']\):\s*\n\s*final_status\s*=\s*["\']failed["\']',
            src,
        )
        self.assertIsNone(
            bad,
            "v3.25: deep 验收失败时不应把 final_status 设为 'failed', "
            "深度验收只是内容质量维度, 不阻塞任务 done 状态",
        )

    def test_deep_fail_no_update_task_status_failed(self):
        """v3.25: 源码不能再有 deep 验收失败分支的 update_task_status(..., 'failed')."""
        src = self._load()
        # v3.25 之前: elif not passed: ... update_task_status(task_id, "failed", ...)
        # 找到 deep_result 块, 检查内部不再调 update_task_status(task_id, "failed"
        m = re.search(
            r'deep_result = run_deep_validation.*?(?=\n        quality_summary = summarize)',
            src,
            re.DOTALL,
        )
        if m:
            block = m.group(0)
            self.assertNotIn(
                'update_task_status(task_id, "failed"',
                block,
                "v3.25: deep 验收块内不应调 update_task_status(..., 'failed')",
            )

    def test_deep_fail_logs_warning(self):
        """v3.25: deep 验收失败时必须 log 警告 (含「不阻塞任务」)."""
        src = self._load()
        self.assertIn("不阻塞任务", src,
                       "v3.25: deep 验收失败必须 log 警告, 含「不阻塞任务」字样")

    def test_deep_unavailable_logs_no_block(self):
        """v3.25: deep 验收 unavailable 也注明不阻塞."""
        src = self._load()
        # 找 unavailable 分支
        m = re.search(
            r'if deep_result\.get\(["\']status["\']\)\s*==\s*["\']unavailable["\']:(.*?)(?=\n        elif|\n        else)',
            src,
            re.DOTALL,
        )
        if m:
            self.assertIn("不阻塞任务", m.group(1),
                           "v3.25: deep 不可用分支也应注明不阻塞")


if __name__ == "__main__":
    unittest.main()
