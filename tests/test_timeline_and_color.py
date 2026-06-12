import unittest
import inspect
import tempfile
from unittest.mock import patch
from unittest.mock import AsyncMock

from autokat.core.editor import generate_script
from autokat.core.perturbation import build_perturbation
from autokat.core.renderer import _needs_hdr_to_sdr
from autokat.core.renderer import (
    _duration_tolerance, _fmt_ass_time, _resolve_final_duration, _segment_render_frames,
    render_simple,
)
from autokat.core.tts import build_phrase_timings, generate_narration
from autokat.core.subtitle_sync import (
    calibrate_phrase_timings, semantic_unit_chunks, split_punctuation_clauses,
)
from autokat.core.timeline import (
    SAMPLE_RATE, apply_integer_timeline, build_target_clock, frames_to_samples,
)


def material(mid, duration=10.0):
    return {
        "id": mid,
        "source_id": mid,
        "path": f"/tmp/{mid}.mp4",
        "duration": duration,
        "type": "video",
        "tags": [],
    }


class TimelineAndColorTests(unittest.TestCase):
    def test_xfade_overhead_and_dynamic_tail_land_on_final_duration(self):
        script = generate_script(
            [{"text": "测试字幕", "start": 0.0, "end": 10.0}],
            material_pool=[material(i) for i in range(1, 20)],
            config={"transition_duration": 0.3, "tail_duration": 0.5},
        )
        clips = script["clips"]
        groups = [clips[i:i + 5] for i in range(0, len(clips), 5)]
        rendered = sum(
            sum(clip["duration"] for clip in group)
            - 0.3 * max(0, len(group) - 1)
            for group in groups
        )
        self.assertAlmostEqual(rendered, 10.5, places=2)
        self.assertEqual(script["narration_duration"], 10.0)
        self.assertEqual(script["final_duration"], 10.5)
        self.assertTrue(clips[-1]["is_tail"])

    def test_script_final_duration_is_authoritative(self):
        self.assertEqual(
            _resolve_final_duration({"final_duration": 10.5}, audio_duration=10.0),
            10.5,
        )

    def test_old_script_is_upgraded_with_silent_tail(self):
        self.assertAlmostEqual(
            _resolve_final_duration({}, audio_duration=10.25),
            10.7333333333,
        )

    def test_frame_tolerance_stays_within_sync_contract(self):
        self.assertEqual(_duration_tolerance(30), 0.02)
        self.assertEqual(_duration_tolerance(60), 0.02)

    def test_phrase_timings_attach_punctuation_to_previous_phrase(self):
        text = "姐妹们今天推荐给大家一款夏日女鞋，这款女鞋舒服百搭"
        boundaries = [
            {"text": char, "start": index * 0.1, "end": (index + 1) * 0.1}
            for index, char in enumerate(text)
        ]
        phrases = build_phrase_timings(boundaries, source_text=text)
        self.assertTrue(any(phrase["text"].endswith("，") for phrase in phrases))
        self.assertFalse(any(phrase["text"].startswith("，") for phrase in phrases))
        self.assertEqual(phrases[0]["start"], 0.0)
        self.assertTrue(all(len(phrase["text"].rstrip("，；：。！？")) <= 20 for phrase in phrases))

    def test_every_target_punctuation_forces_a_caption_break(self):
        text = "第一句很短，第二句结束。真的可以吗？当然可以！"
        boundaries = [
            {"text": char, "start": index * 0.05, "end": (index + 1) * 0.05}
            for index, char in enumerate(text)
        ]
        phrases = build_phrase_timings(boundaries, source_text=text)
        endings = [phrase["text"][-1] for phrase in phrases if phrase["text"][-1] in "，。！？"]
        self.assertEqual(endings, ["，", "。", "？", "！"])

    def test_non_spoken_punctuation_does_not_break_word_boundary_alignment(self):
        text = "通勤、逛街或朋友聚会，一双时尚女鞋更利落。"
        spoken = "通勤逛街或朋友聚会一双时尚女鞋更利落"
        boundaries = [
            {"text": char, "start": index * 0.05, "end": (index + 1) * 0.05}
            for index, char in enumerate(spoken)
        ]
        phrases = build_phrase_timings(boundaries, source_text=text)
        self.assertEqual("".join(phrase["text"] for phrase in phrases), text)
        self.assertTrue(phrases[-1]["text"].endswith("。"))

    def test_missing_or_unaligned_boundaries_fail(self):
        with self.assertRaises(ValueError):
            build_phrase_timings([], source_text="测试。")
        with self.assertRaises(ValueError):
            build_phrase_timings(
                [{"text": "不匹配", "start": 0, "end": 1}],
                source_text="测试。",
            )

    @patch("autokat.core.tts.time.sleep")
    @patch("autokat.core.tts.prepare_pcm_and_calibrate")
    @patch("autokat.core.tts._generate_tts_with_boundaries", new_callable=AsyncMock)
    def test_tts_retries_transient_failures_before_accepting_valid_boundaries(
        self, generate_with_boundaries, prepare_pcm, sleep,
    ):
        generate_with_boundaries.side_effect = [
            RuntimeError("temporary network failure"),
            (1.2, []),
            (
                1.2,
                [
                    {"text": "测", "start": 0.1, "end": 0.3},
                    {"text": "试", "start": 0.3, "end": 0.5},
                ],
            ),
        ]
        prepare_pcm.side_effect = lambda path, timings, duration: (path, timings, duration)
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("autokat.core.tts.TTS_DIR", __import__("pathlib").Path(tmpdir)):
                result = generate_narration(
                    "测试。", voice="zh-CN-XiaoxiaoNeural",
                    output_name="retry", lang="zh",
                )
        self.assertEqual(generate_with_boundaries.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(result["sentences"][0]["text"], "测试。")

    def test_normal_chinese_punctuation_drives_semantic_clauses(self):
        self.assertEqual(
            split_punctuation_clauses("通勤、逛街都合适；搭配简单：状态更轻松。真的？可以！"),
            ["通勤、逛街都合适；", "搭配简单：", "状态更轻松。", "真的？", "可以！"],
        )

    def test_semantic_chunks_do_not_split_quoted_product_or_quantity_unit(self):
        for text, protected in (
            ("姐妹们今天推荐“玛丽珍珠鞋”搭配日常通勤场景真的非常轻松自在舒适自然", "“玛丽珍珠鞋”"),
            ("今天准备了12双时尚女鞋适合日常通勤逛街聚会也能轻松搭配出门", "12双"),
        ):
            units = [
                {"text": char, "chars": 1, "start": index * 0.05, "end": (index + 1) * 0.05}
                for index, char in enumerate(text)
            ]
            chunks = ["".join(unit["text"] for unit in chunk)
                      for chunk in semantic_unit_chunks(units, text, max_chars=20)]
            self.assertEqual(sum(protected in chunk for chunk in chunks), 1)
            self.assertTrue(all(len(chunk) <= 20 for chunk in chunks))

    def test_pcm_vad_calibration_keeps_real_pause_empty(self):
        phrases = [
            {"text": "第一句，", "start": 0.1, "end": 1.0},
            {"text": "第二句。", "start": 1.7, "end": 2.6},
        ]
        calibrated = calibrate_phrase_timings(
            phrases, [(0.2, 1.05), (1.8, 2.65)], narration_duration=3.0,
        )
        self.assertLessEqual(calibrated[0]["end"], 1.05)
        self.assertGreater(calibrated[1]["start"] - calibrated[0]["end"], 0.5)
        self.assertEqual(calibrated[0]["timing_source"], "word_boundary+pcm_vad")

    def test_integer_clock_matches_frames_and_samples(self):
        for fps in (30, 60):
            clock = build_target_clock(19.517, fps, 0.5)
            self.assertEqual(
                clock["target_audio_samples"],
                frames_to_samples(clock["target_video_frames"], fps),
            )
            self.assertLessEqual(
                abs(clock["final_duration"] - 20.017),
                0.5 / fps,
            )
            self.assertEqual(clock["sample_rate"], SAMPLE_RATE)

    def test_clip_timeline_uses_integer_frames_without_drift(self):
        script = {
            "clips": [
                {"start_time": 0, "end_time": 3.111, "duration": 3.111},
                {"start_time": 3.111, "end_time": 9.777, "duration": 6.666},
            ],
            "transition_duration": 0.3,
        }
        apply_integer_timeline(script, 9.5, 30, 0.5)
        self.assertEqual(script["clips"][-1]["end_frame"], script["target_video_frames"])
        self.assertTrue(all(isinstance(c["duration_frames"], int) for c in script["clips"]))
        self.assertEqual(script["clips"][1]["transition_end_frame"], script["clips"][1]["start_frame"])
        self.assertEqual(
            script["clips"][1]["transition_start_frame"],
            script["clips"][1]["start_frame"] - script["clips"][1]["transition_frames"],
        )

    def test_final_segment_has_dynamic_render_guard_frames(self):
        clips = [{"duration_frames": 30}, {"duration_frames": 24}]
        self.assertEqual(_segment_render_frames(clips[0], 0, len(clips), 30), 30)
        self.assertEqual(_segment_render_frames(clips[1], 1, len(clips), 30), 26)

    def test_renderer_does_not_use_shortest(self):
        self.assertNotIn("-shortest", inspect.getsource(render_simple))

    def test_ass_timestamp_uses_centiseconds_required_by_libass(self):
        self.assertEqual(_fmt_ass_time(0.106667), "0:00:00.11")
        self.assertEqual(_fmt_ass_time(62.248), "0:01:02.25")
        self.assertEqual(_fmt_ass_time(3600.999), "1:00:01.00")

    def test_wizard_error_handler_does_not_overwrite_original_error(self):
        from autokat.ui.main_window import MainWindow
        source = inspect.getsource(MainWindow._on_wizard_gen_error)
        self.assertNotIn("for msg in _log_drain()", source)

    def test_encoding_randomization_is_disabled(self):
        perturbation = build_perturbation("high")
        self.assertNotIn("crf", perturbation)
        self.assertNotIn("bitrate_mbps", perturbation)
        self.assertNotIn("gop_size", perturbation)

    @patch("autokat.core.renderer.get_media_info")
    def test_hdr_metadata_requires_sdr_conversion(self, get_media_info):
        get_media_info.return_value = {
            "streams": [{
                "codec_type": "video",
                "color_primaries": "bt2020",
                "color_space": "bt2020nc",
                "color_transfer": "arib-std-b67",
            }]
        }
        self.assertTrue(_needs_hdr_to_sdr("/tmp/hdr.mov"))

    @patch("autokat.core.renderer.get_media_info")
    def test_bt709_metadata_passes_through(self, get_media_info):
        get_media_info.return_value = {
            "streams": [{
                "codec_type": "video",
                "color_primaries": "bt709",
                "color_space": "bt709",
                "color_transfer": "bt709",
            }]
        }
        self.assertFalse(_needs_hdr_to_sdr("/tmp/sdr.mp4"))


if __name__ == "__main__":
    unittest.main()
