"""v3.21 守护测试: TTS 入口 sanitize + 短空段短路 + chunked 回退."""
import unittest
from unittest.mock import patch

from autokat.core.tts import (
    _sanitize_for_tts,
    _split_for_chunked_tts,
    generate_narration,
)


class SanitizeForTTSTests(unittest.TestCase):
    """v3.21: _sanitize_for_tts 覆盖空段/装饰字符场景."""

    def test_normal_text_unchanged(self):
        s, ok = _sanitize_for_tts("今天天气真好, 我们去公园散步吧。")
        self.assertTrue(ok)
        self.assertEqual(s, "今天天气真好, 我们去公园散步吧。")

    def test_hashtag_stripped_to_space(self):
        s, ok = _sanitize_for_tts("女鞋 #经典单品 #品质保证 让你走在时尚尖端。")
        self.assertTrue(ok)
        self.assertIn("经典单品", s)
        self.assertNotIn("#", s)

    def test_bracket_bookname_stripped(self):
        s, ok = _sanitize_for_tts("【种草】玛丽珍珠 #天然宝石 温润光泽")
        self.assertTrue(ok)
        self.assertNotIn("【", s)
        self.assertNotIn("】", s)
        self.assertNotIn("#", s)
        self.assertIn("玛丽珍珠", s)

    def test_emoji_preserved_in_normal_text(self):
        s, ok = _sanitize_for_tts("🌸 玛丽珍珠让你优雅, 温润光泽像月光。")
        self.assertTrue(ok)
        self.assertIn("玛丽珍珠", s)
        self.assertIn("🌸", s)

    def test_empty_string_returns_empty(self):
        s, ok = _sanitize_for_tts("")
        self.assertFalse(ok)
        self.assertEqual(s, "")

    def test_whitespace_only_returns_empty(self):
        s, ok = _sanitize_for_tts("   \n  \t  ")
        self.assertFalse(ok)
        self.assertEqual(s, "")

    def test_pure_symbols_returns_empty(self):
        s, ok = _sanitize_for_tts("##@@%%**")
        self.assertFalse(ok)
        self.assertEqual(s, "")

    def test_pure_emoji_returns_empty(self):
        s, ok = _sanitize_for_tts("😀😁😂🤣")
        self.assertFalse(ok)
        self.assertEqual(s, "")

    def test_single_char_returns_empty(self):
        s, ok = _sanitize_for_tts("a")
        self.assertFalse(ok)

    def test_short_chinese_passes(self):
        s, ok = _sanitize_for_tts("春夏女鞋")
        self.assertTrue(ok)
        self.assertEqual(s, "春夏女鞋")

    def test_mixed_punctuation_handled(self):
        s, ok = _sanitize_for_tts("春夏女鞋   经典百搭,通勤轻松。")
        self.assertTrue(ok)
        self.assertNotIn("  ", s)


class GenerateNarrationEmptyRejectTests(unittest.TestCase):
    """v3.21: generate_narration 入口拒绝空文本 (不再 9 次空跑)."""

    def test_empty_string_raises_valueerror(self):
        with self.assertRaises(ValueError) as ctx:
            generate_narration("", lang="zh", output_name="v321_empty")
        msg = str(ctx.exception)
        self.assertIn("无可发音内容", msg)
        self.assertIn("预览", msg)

    def test_pure_symbols_raises_valueerror(self):
        with self.assertRaises(ValueError) as ctx:
            generate_narration("##@@%%**", lang="zh", output_name="v321_symbols")
        self.assertIn("无可发音内容", str(ctx.exception))
        self.assertIn("##@@%%**", str(ctx.exception))

    def test_whitespace_only_raises_valueerror(self):
        with self.assertRaises(ValueError):
            generate_narration("   \n\t  ", lang="zh", output_name="v321_spaces")

    def test_emoji_only_raises_valueerror(self):
        with self.assertRaises(ValueError):
            generate_narration("😀😁😂", lang="zh", output_name="v321_emoji")

    def test_no_more_9_attempts_for_empty(self):
        with patch("autokat.core.tts._generate_tts_with_boundaries") as mock_tts:
            with self.assertRaises(ValueError):
                generate_narration("", lang="zh", output_name="v321_no_retry")
            mock_tts.assert_not_called()


class SplitForChunkedTTSTests(unittest.TestCase):
    """v3.21: _split_for_chunked_tts 按标点切分子段."""

    def test_short_text_returns_single_chunk(self):
        chunks = _split_for_chunked_tts("今天天气真好。", max_chars=80)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], "今天天气真好。")

    def test_force_split_short_multi_sentence_text(self):
        text = (
            "没想到女鞋还能这样，真的是打开新世界了。"
            "春夏时节，一双合适的鞋能让整个人状态都松弛自然起来。"
            "百搭的设计不挑任何风格，通勤逛街约会都能轻松切换。"
        )
        chunks = _split_for_chunked_tts(
            text, max_chars=80, force_split=True,
        )
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(chunk) <= 80 for chunk in chunks))

    def test_long_text_split_by_period(self):
        text = "没有想到女鞋还能这样, 真的是打开新世界了。" * 20
        chunks = _split_for_chunked_tts(text, max_chars=80)
        for c in chunks:
            self.assertLessEqual(len(c), 80)
        self.assertGreater(len(chunks), 1)

    def test_empty_chunks_filtered(self):
        chunks = _split_for_chunked_tts("短句。短句。", max_chars=80)
        for c in chunks:
            self.assertGreaterEqual(len(c.strip()), 5)


class GenerateNarrationRecoveryTests(unittest.TestCase):
    """短文本整段失败后也必须进入强制拆句回退。"""

    @patch("autokat.core.tts.prepare_pcm_and_calibrate")
    @patch("autokat.core.tts._generate_narration_chunked")
    @patch("autokat.core.tts._generate_tts_with_boundaries")
    @patch("autokat.core.tts.time.sleep")
    def test_short_text_uses_chunked_fallback_after_nine_failures(
        self, _sleep, mock_generate, mock_chunked, _calibrate,
    ):
        mock_generate.side_effect = RuntimeError("No audio was received")
        mock_chunked.return_value = {
            "audio_path": "/tmp/recovered.mp3",
            "total_duration": 12.0,
            "sentences": [{"index": 0, "text": "恢复成功。", "start": 0, "end": 12}],
        }
        text = (
            "没想到女鞋还能这样，真的是打开新世界了。"
            "春夏时节，一双合适的鞋能让整个人状态都松弛自然起来。"
            "百搭的设计不挑任何风格，通勤逛街约会都能轻松切换。"
        )

        result = generate_narration(
            text, lang="zh", output_name="v321_short_recovery",
        )

        self.assertEqual(mock_generate.call_count, 9)
        chunks = mock_chunked.call_args.args[0]
        self.assertGreater(len(chunks), 1)
        self.assertEqual(result["audio_path"], "/tmp/recovered.mp3")


if __name__ == "__main__":
    unittest.main()
