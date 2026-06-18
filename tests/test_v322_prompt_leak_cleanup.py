import unittest

from autokat.core.writer import _clean_result


class PromptLeakCleanupTests(unittest.TestCase):
    def test_internal_capability_example_is_removed(self):
        text = (
            "轻松自在的穿搭，让生活更精彩。"
            "| 用例: 女鞋、女鞋、温馨场景、轻松互动"
        )
        self.assertEqual(
            _clean_result(text, topic="女鞋"),
            "轻松自在的穿搭，让生活更精彩。",
        )

    def test_end_marker_is_removed(self):
        self.assertEqual(
            _clean_result("无论春夏秋冬，都能找到自己的风格。(结束)", topic="女鞋"),
            "无论春夏秋冬，都能找到自己的风格。",
        )


if __name__ == "__main__":
    unittest.main()
