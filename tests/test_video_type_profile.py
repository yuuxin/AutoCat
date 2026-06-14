"""Tests for the per-video-type rhythm / transition profile table.

These tests cover:
* the shape and contents of VIDEO_TYPE_PROFILES
* video_type_profile() lookup and fallback semantics
* the profile wiring into _plan_shots_by_frames (shot duration bounds)
* the profile wiring into _random_transition (pool sampling)
* the profile_version / active_profile fields written to scripts
"""
import unittest
from collections import Counter

from autokat.core.editor import (
    INTENT_VERSION,
    PLANNER_VERSION,
    PROFILE_VERSION,
    TRANSITIONS,
    VIDEO_TYPE_PROFILES,
    _plan_shots_by_frames,
    _random_transition,
    generate_script,
    video_type_profile,
)


SUPPORTED_TYPES = (
    "product_recommendation",
    "talking_explanation",
    "atmosphere",
    "music_beat",
    "random_mix",
)


class ProfileTableShapeTests(unittest.TestCase):
    def test_every_supported_type_has_required_keys(self):
        required = {
            "shot_min", "shot_max", "shots_per_minute",
            "transition_pool", "transition_pick_prob",
            "min_subtitle_gap", "semantic_weight", "visual_weight",
        }
        for vt in SUPPORTED_TYPES:
            self.assertIn(vt, VIDEO_TYPE_PROFILES, f"missing profile for {vt}")
            prof = VIDEO_TYPE_PROFILES[vt]
            missing = required - set(prof.keys())
            self.assertFalse(missing, f"{vt} missing keys: {missing}")

    def test_shot_min_less_than_shot_max(self):
        for vt, prof in VIDEO_TYPE_PROFILES.items():
            self.assertLess(
                prof["shot_min"], prof["shot_max"],
                f"{vt} has shot_min >= shot_max",
            )

    def test_shots_per_minute_matches_shot_bounds(self):
        for vt, prof in VIDEO_TYPE_PROFILES.items():
            target = 60.0 / prof["shots_per_minute"]
            self.assertGreaterEqual(
                target, prof["shot_min"] * 0.5,
                f"{vt} target {target:.2f} far below shot_min {prof['shot_min']}",
            )
            self.assertLessEqual(
                target, prof["shot_max"] * 1.5,
                f"{vt} target {target:.2f} far above shot_max {prof['shot_max']}",
            )

    def test_transition_pick_prob_within_unit_interval(self):
        for vt, prof in VIDEO_TYPE_PROFILES.items():
            self.assertGreaterEqual(prof["transition_pick_prob"], 0.0)
            self.assertLessEqual(prof["transition_pick_prob"], 1.0)

    def test_transition_pool_entries_are_real_transitions(self):
        for vt, prof in VIDEO_TYPE_PROFILES.items():
            for name in prof["transition_pool"]:
                self.assertIn(
                    name, TRANSITIONS,
                    f"{vt} transition_pool contains unknown {name!r}",
                )

    def test_auto_alias_points_to_random_mix(self):
        self.assertIs(
            VIDEO_TYPE_PROFILES["auto"],
            VIDEO_TYPE_PROFILES["random_mix"],
        )

    def test_profile_version_is_pinned(self):
        self.assertEqual(PROFILE_VERSION, "rhythm-profile-v1")


class VideoTypeProfileLookupTests(unittest.TestCase):
    def test_every_supported_type_round_trips(self):
        for vt in SUPPORTED_TYPES:
            self.assertIs(video_type_profile(vt), VIDEO_TYPE_PROFILES[vt])

    def test_auto_returns_random_mix_profile(self):
        self.assertIs(
            video_type_profile("auto"),
            VIDEO_TYPE_PROFILES["random_mix"],
        )

    def test_unknown_string_falls_back_to_random_mix(self):
        self.assertIs(
            video_type_profile("novel_type"),
            VIDEO_TYPE_PROFILES["random_mix"],
        )

    def test_empty_and_none_fall_back_to_random_mix(self):
        for value in ("", None):
            self.assertIs(
                video_type_profile(value),
                VIDEO_TYPE_PROFILES["random_mix"],
            )

    def test_lookup_is_case_insensitive(self):
        self.assertIs(
            video_type_profile("MUSIC_BEAT"),
            VIDEO_TYPE_PROFILES["music_beat"],
        )


class PlannerProfileWiringTests(unittest.TestCase):
    def _sentences(self, count=20, gap=1.2):
        return [
            {"text": f"第{i}句", "start": i * gap, "end": (i + 1) * gap}
            for i in range(count)
        ]

    def test_music_beat_uses_short_shots(self):
        sentences = self._sentences()
        shots = _plan_shots_by_frames(sentences, 30, len(sentences) * 1.2, "music_beat")
        self.assertTrue(shots, "planner produced no shots")
        durations = [
            (s[-1]["end"] - s[0]["start"]) for s in shots
        ]
        self.assertLessEqual(
            max(durations), VIDEO_TYPE_PROFILES["music_beat"]["shot_max"] + 0.01,
            f"music_beat shot_max exceeded: {max(durations)}",
        )

    def test_atmosphere_uses_long_shots(self):
        sentences = self._sentences()
        shots = _plan_shots_by_frames(sentences, 30, len(sentences) * 1.2, "atmosphere")
        self.assertTrue(shots)
        avg_duration = sum(s[-1]["end"] - s[0]["start"] for s in shots) / len(shots)
        self.assertGreaterEqual(
            avg_duration, VIDEO_TYPE_PROFILES["atmosphere"]["shot_min"],
        )

    def test_talking_explanation_does_not_collapse_to_subsecond(self):
        sentences = self._sentences(count=12, gap=0.8)
        shots = _plan_shots_by_frames(sentences, 30, len(sentences) * 0.8,
                                      "talking_explanation")
        for shot in shots:
            duration = shot[-1]["end"] - shot[0]["start"]
            self.assertGreaterEqual(
                duration, VIDEO_TYPE_PROFILES["talking_explanation"]["shot_min"] - 0.5,
                f"talking_explanation shot {duration:.2f}s shorter than profile floor",
            )

    def test_unknown_video_type_uses_random_mix_profile(self):
        sentences = self._sentences()
        shots = _plan_shots_by_frames(sentences, 30, len(sentences) * 1.2, "mystery_type")
        self.assertTrue(shots)


class RandomTransitionProfileTests(unittest.TestCase):
    def test_no_profile_uses_full_transition_space(self):
        counter = Counter(_random_transition() for _ in range(2000))
        self.assertGreaterEqual(len(counter), 45)

    def test_profile_with_zero_probability_still_samples_full_space(self):
        prof = dict(VIDEO_TYPE_PROFILES["music_beat"])
        prof["transition_pick_prob"] = 0.0
        counter = Counter(_random_transition(prof) for _ in range(2000))
        self.assertGreaterEqual(len(counter), 45)

    def test_high_pick_prob_keeps_transitions_inside_pool(self):
        prof = VIDEO_TYPE_PROFILES["talking_explanation"]
        pool = set(prof["transition_pool"])
        counter = Counter(_random_transition(prof) for _ in range(2000))
        in_pool = sum(counter[t] for t in pool)
        self.assertGreater(in_pool / 2000.0, 0.80)

    def test_low_pick_prob_blends_toward_full_space(self):
        prof = VIDEO_TYPE_PROFILES["random_mix"]
        pool = set(prof["transition_pool"])
        counter = Counter(_random_transition(prof) for _ in range(4000))
        in_pool = sum(counter[t] for t in pool)
        self.assertGreater(in_pool / 4000.0, 0.50)
        self.assertLess(in_pool / 4000.0, 0.70)


class GenerateScriptProfileMetadataTests(unittest.TestCase):
    def _pool(self, n=12):
        return [
            {
                "id": i, "path": f"/tmp/m{i}.mp4", "duration": 10.0,
                "width": 1080, "height": 1920, "tags": [],
                "capability_summary": "", "source_id": i, "type": "video",
            }
            for i in range(1, n + 1)
        ]

    def _sentences(self, count=10, gap=1.5):
        return [
            {"text": f"第{i}句", "start": i * gap, "end": (i + 1) * gap}
            for i in range(count)
        ]

    def test_script_records_profile_version_and_active_profile(self):
        sentences = self._sentences()
        script = generate_script(
            sentences, material_pool=self._pool(),
            config={"fps": 30, "transition_duration": 0, "tail_duration": 0,
                    "source_safety_margin": 0, "video_type": "music_beat"},
        )
        self.assertEqual(script.get("profile_version"), PROFILE_VERSION)
        self.assertIn("active_profile", script)
        active = script["active_profile"]
        self.assertEqual(active["shot_min"], VIDEO_TYPE_PROFILES["music_beat"]["shot_min"])
        self.assertEqual(active["shot_max"], VIDEO_TYPE_PROFILES["music_beat"]["shot_max"])
        self.assertEqual(script.get("planner_version"), PLANNER_VERSION)
        self.assertEqual(script.get("intent_version"), INTENT_VERSION)

    def test_script_video_type_is_resolved_for_auto(self):
        sentences = self._sentences()
        sentences[0]["text"] = "推荐几款好物"
        script = generate_script(
            sentences, material_pool=self._pool(),
            config={"fps": 30, "transition_duration": 0, "tail_duration": 0,
                    "source_safety_margin": 0, "video_type": "auto",
                    "narration_text": "推荐几款好物"},
        )
        self.assertEqual(script.get("video_type"), "product_recommendation")
        self.assertEqual(
            script["active_profile"]["shot_min"],
            VIDEO_TYPE_PROFILES["product_recommendation"]["shot_min"],
        )

    def test_long_form_safeguard_does_not_clobber_profile_max(self):
        sentences = [{"text": f"第{i}句", "start": i * 1.2, "end": (i + 1) * 1.2}
                     for i in range(60)]
        script = generate_script(
            sentences, material_pool=self._pool(n=20),
            config={"fps": 30, "transition_duration": 0, "tail_duration": 0,
                    "source_safety_margin": 0, "video_type": "atmosphere"},
        )
        self.assertEqual(
            script["active_profile"]["shot_max"],
            VIDEO_TYPE_PROFILES["atmosphere"]["shot_max"],
        )


if __name__ == "__main__":
    unittest.main()
