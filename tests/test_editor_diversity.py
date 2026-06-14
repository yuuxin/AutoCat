import unittest
import copy

from autokat.core.editor import _pick_material, generate_batch, generate_script


def material(mid, source_id=None, duration=1.0, tags=None):
    return {
        "id": mid,
        "source_id": source_id or mid,
        "path": f"/tmp/{mid}.mp4",
        "duration": duration,
        "type": "video",
        "tags": tags or [],
    }


class EditorDiversityTests(unittest.TestCase):
    def test_integer_rhythm_planner_keeps_subtitle_input_immutable(self):
        sentences = [
            {"text": f"第{i}句，", "start": i * 2.5, "end": (i + 1) * 2.5}
            for i in range(12)
        ]
        original = copy.deepcopy(sentences)
        script = generate_script(
            sentences,
            material_pool=[material(i, duration=12) for i in range(1, 20)],
            config={"fps": 30, "transition_duration": 0, "tail_duration": 0,
                    "source_safety_margin": 0},
        )
        self.assertEqual(sentences, original)
        self.assertEqual(script["planner_version"], "integer-rhythm-v1")
        self.assertTrue(all(isinstance(shot["start_frame"], int) for shot in script["planned_shots"]))

    def test_long_video_uses_stable_shots_and_no_subsecond_tail_fragment(self):
        sentences = [
            {"text": f"第{i}句，", "start": i * 1.2, "end": (i + 1) * 1.2}
            for i in range(30)
        ]
        script = generate_script(
            sentences,
            material_pool=[material(i, duration=20) for i in range(1, 30)],
            config={"fps": 30, "transition_duration": 0, "tail_duration": 0,
                    "source_safety_margin": 0},
        )
        durations = [
            (shot["end_frame"] - shot["start_frame"]) / 30
            for shot in script["planned_shots"]
        ]
        self.assertTrue(any(duration >= 3 for duration in durations))
        self.assertGreaterEqual(durations[-1], 1.0)

    def test_unused_short_slice_beats_reused_matching_long_slice(self):
        pool = [
            material(1, duration=10, tags=["match"]),
            material(2, duration=1),
        ]
        state = {
            "enable_diversity": True,
            "usage_count": {1: 1, 2: 0},
            "source_usage_count": {1: 1, 2: 0},
            "recent_sets": [],
            "recent_source_sets": [],
            "max_uses": 5,
        }

        for _ in range(20):
            picked = _pick_material(pool, set(), 5, ["match"], state)
            self.assertEqual(picked["id"], 2)

    def test_local_subtitle_intent_selects_matching_capability(self):
        pool = [
            material(1, duration=10, tags=["风景"]),
            material(2, duration=10, tags=["女鞋", "细节"]),
        ]
        script = generate_script(
            [{"text": "看看这双女鞋的设计细节。", "start": 0, "end": 3}],
            material_pool=pool,
            config={
                "fps": 30, "transition_duration": 0, "tail_duration": 0,
                "source_safety_margin": 0, "narration_text": "",
            },
            state={"selection_top_k": 1},
        )
        self.assertEqual(script["clips"][0]["material_id"], 2)
        self.assertEqual(script["clips"][0]["semantic_match_level"], "local_intent")

    def test_semantic_matching_falls_back_to_quality_without_changing_timeline(self):
        pool = [
            {**material(1, duration=10), "quality_score": 0.1},
            {**material(2, duration=10), "quality_score": 0.9},
        ]
        script = generate_script(
            [{"text": "完全未知的主题。", "start": 0, "end": 3}],
            material_pool=pool,
            config={
                "fps": 30, "transition_duration": 0, "tail_duration": 0,
                "source_safety_margin": 0,
            },
            state={"selection_top_k": 1},
        )
        self.assertEqual(script["clips"][0]["material_id"], 2)
        self.assertEqual(script["clips"][0]["semantic_match_level"], "quality_fallback")
        self.assertEqual(script["target_video_frames"], 90)

    def test_batch_covers_all_slices_before_reuse(self):
        pool = [material(i) for i in range(1, 13)]
        sentences = [{"text": "test", "start": 0, "end": 6}]

        batch = generate_batch(
            sentences,
            count=2,
            material_pool=pool,
            config={"min_shot_duration": 6, "enable_diversity": True,
                    "transition_duration": 0, "tail_duration": 0,
                    "source_safety_margin": 0},
        )
        first = {c["material_id"] for c in batch[0]["clips"]}
        second = {c["material_id"] for c in batch[1]["clips"]}

        self.assertTrue(first.isdisjoint(second))
        self.assertEqual(batch[0]["diversity_report"]["slice_coverage"], 1.0)
        self.assertEqual(batch[0]["diversity_report"]["max_slice_uses"], 1)

    def test_one_video_spreads_across_source_videos(self):
        pool = [
            material(source * 10 + index, source_id=source)
            for source in range(1, 5)
            for index in range(3)
        ]
        sentences = [{"text": "test", "start": 0, "end": 4}]

        batch = generate_batch(
            sentences,
            count=1,
            material_pool=pool,
            config={"min_shot_duration": 4, "enable_diversity": True,
                    "transition_duration": 0, "tail_duration": 0,
                    "source_safety_margin": 0},
        )
        sources = {c["source_id"] for c in batch[0]["clips"]}
        self.assertEqual(len(sources), 4)

    def test_sentence_groups_keep_shared_diversity_state(self):
        pool = [material(i) for i in range(1, 9)]
        groups = [
            [{"text": "a", "start": 0, "end": 4}],
            [{"text": "b", "start": 0, "end": 4}],
        ]

        batch = generate_batch(
            groups[0],
            count=2,
            material_pool=pool,
            sentence_groups=groups,
            config={"min_shot_duration": 4, "enable_diversity": True,
                    "transition_duration": 0, "tail_duration": 0,
                    "source_safety_margin": 0},
        )
        first = {c["material_id"] for c in batch[0]["clips"]}
        second = {c["material_id"] for c in batch[1]["clips"]}
        self.assertTrue(first.isdisjoint(second))

    def test_usage_stays_balanced_across_reuse_rounds(self):
        pool = [material(i, source_id=((i - 1) // 5) + 1) for i in range(1, 21)]
        sentences = [{"text": "test", "start": 0, "end": 5}]

        batch = generate_batch(
            sentences,
            count=8,
            material_pool=pool,
            config={"min_shot_duration": 5, "enable_diversity": True,
                    "transition_duration": 0, "tail_duration": 0,
                    "source_safety_margin": 0},
        )
        usage = {}
        for script in batch:
            for clip in script["clips"]:
                mid = clip["material_id"]
                usage[mid] = usage.get(mid, 0) + 1

        self.assertEqual(len(usage), len(pool))
        self.assertLessEqual(max(usage.values()) - min(usage.values()), 1)

    def test_combination_similarity_stays_low_when_pool_is_sufficient(self):
        pool = [material(i) for i in range(1, 25)]
        sentences = [{"text": "test", "start": 0, "end": 4}]

        batch = generate_batch(
            sentences,
            count=6,
            material_pool=pool,
            config={
                "min_shot_duration": 4,
                "enable_diversity": True,
                "diversity_retry_attempts": 6,
                "diversity_jaccard_target": 0.5,
                "transition_duration": 0,
                "tail_duration": 0,
                "source_safety_margin": 0,
            },
        )
        report = batch[0]["diversity_report"]
        self.assertLessEqual(report["max_slice_jaccard"], 0.5)


if __name__ == "__main__":
    unittest.main()
