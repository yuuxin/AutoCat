import unittest

from autokat.core.content_sync import (
    align_asr_to_subtitles, correct_subtitles_from_alignment, evaluate_ocr_samples,
    punctuation_breaks_valid,
)


def units(text, start=0.2, step=0.1):
    return [
        {"text": char, "start": start + index * step, "end": start + (index + 1) * step}
        for index, char in enumerate(text)
    ]


class ContentSyncTests(unittest.TestCase):
    def test_matching_asr_and_ocr_pass(self):
        subtitles = [{"text": "时尚女鞋。", "start": 0.12, "end": 0.62}]
        aligned = align_asr_to_subtitles(units("时尚女鞋", start=0.2), subtitles)
        self.assertTrue(aligned[0]["passed"])
        checked = evaluate_ocr_samples(subtitles, [{"time": 0.3, "ocr_text": "时尚女鞋。"}])
        self.assertTrue(checked[0]["passed"])

    def test_early_late_and_wrong_content_fail(self):
        subtitles = [{"text": "时尚女鞋。", "start": 0.8, "end": 1.3}]
        self.assertFalse(align_asr_to_subtitles(units("时尚女鞋", start=0.2), subtitles)[0]["passed"])
        self.assertFalse(evaluate_ocr_samples(
            subtitles, [{"time": 0.9, "ocr_text": "完全错误字幕"}],
        )[0]["passed"])

    def test_asr_content_mismatch_and_missing_ocr_fail(self):
        subtitles = [{"text": "时尚女鞋。", "start": 0.12, "end": 0.72}]
        self.assertFalse(align_asr_to_subtitles(
            units("完全无关", start=0.2), subtitles,
        )[0]["passed"])
        self.assertFalse(evaluate_ocr_samples(
            subtitles, [{"time": 0.4, "ocr_text": ""}],
        )[0]["passed"])

    def test_previous_caption_visible_in_gap_fails(self):
        subtitles = [
            {"text": "第一句，", "start": 0.1, "end": 0.8},
            {"text": "第二句。", "start": 1.2, "end": 1.8},
        ]
        checked = evaluate_ocr_samples(subtitles, [{"time": 1.0, "ocr_text": "第一句，"}])
        self.assertFalse(checked[0]["passed"])

    def test_source_background_text_is_ignored_but_current_caption_is_required(self):
        subtitles = [{"text": "当前中文字幕。", "start": 0.2, "end": 1.0}]
        checked = evaluate_ocr_samples(subtitles, [
            {"time": 0.0, "ocr_text": "The Lonely Ones 2026"},
            {"time": 0.5, "ocr_text": "hard worker 当前中文字幕。"},
        ])
        self.assertTrue(all(item["passed"] for item in checked))

    def test_failed_alignment_can_be_corrected_for_one_retry(self):
        subtitles = [{"text": "时尚女鞋。", "start": 0.8, "end": 1.3}]
        alignment = align_asr_to_subtitles(units("时尚女鞋", start=0.2), subtitles)
        corrected = correct_subtitles_from_alignment(subtitles, alignment)
        self.assertAlmostEqual(corrected[0]["start"], 0.12)
        self.assertAlmostEqual(corrected[0]["end"], 0.6)
        self.assertEqual(corrected[0]["timing_source"], "final_mp4_asr_retry")

    def test_punctuation_break_validation(self):
        subtitles = [{"text": "第一句，"}, {"text": "第二句；"}, {"text": "结束。"}]
        self.assertTrue(punctuation_breaks_valid("第一句，第二句；结束。", subtitles))
        self.assertFalse(punctuation_breaks_valid("第一句，第二句；结束。", [{"text": "第一句第二句；"}, {"text": "结束。"}]))


if __name__ == "__main__":
    unittest.main()
