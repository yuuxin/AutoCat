import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autokat.core.batch_planner import BatchPlanner
from autokat.core.renderer import (
    _adaptive_worker_count, _compose_cached_segments, _retarget_frozen_clips,
)
from autokat.core.slice_cache import (
    CACHE_VERSION, SliceCache, build_cache_key, cached_segment, prewarm_scripts,
)
from autokat.models import db


class SliceCacheTests(unittest.TestCase):
    def test_cache_version_invalidates_audio_polluted_entries(self):
        self.assertEqual(CACHE_VERSION, "slice-v5-video-only")

    def test_same_key_builds_once_and_hits_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            with patch.object(db, "DB_DIR", root / "tasks"), patch.object(
                db, "DB_PATH", root / "tasks" / "autokat.db"
            ):
                db.init_db()
                cache = SliceCache(root / "cache")
                key = build_cache_key(str(source), 0, 90, 30)
                builds = []

                def builder(path):
                    builds.append(1)
                    Path(path).write_bytes(b"cached")

                first = cache.build(key, builder)
                self.assertEqual(cache.peek(key), first)
                conn = db.get_conn()
                hit_count_after_peek = conn.execute(
                    "SELECT hit_count FROM cache_entries WHERE cache_key=?", (key,)
                ).fetchone()["hit_count"]
                conn.close()
                second = cache.build(key, builder)
                conn = db.get_conn()
                row = conn.execute(
                    "SELECT hit_count,build_count FROM cache_entries WHERE cache_key=?", (key,)
                ).fetchone()
                conn.close()
        self.assertEqual(first, second)
        self.assertEqual(len(builds), 1)
        self.assertEqual(hit_count_after_peek, 0)
        self.assertGreaterEqual(row["hit_count"], 1)
        self.assertEqual(row["build_count"], 1)

    def test_batch_manifest_reports_hotspots(self):
        scripts = [{
            "fps": 30,
            "clips": [
                {"source_path": "/a.mp4", "offset": 0, "duration_frames": 90},
                {"source_path": "/a.mp4", "offset": 0, "duration_frames": 90},
            ],
        }]
        manifest = BatchPlanner().build_manifest(scripts)
        self.assertEqual(manifest["references"], 2)
        self.assertEqual(manifest["unique_slices"], 1)
        self.assertEqual(manifest["hotspots"][0]["references"], 2)
        self.assertEqual(manifest["estimated_hit_rate"], 0.5)

    @patch("autokat.core.slice_cache.cached_segment")
    @patch("autokat.core.slice_cache.SliceCache.peek", return_value=None)
    def test_prewarm_builds_each_unique_segment_once(self, _peek, cached):
        cached.return_value = (Path("/tmp/cached.mp4"), "key")
        scripts = [{
            "fps": 30, "perturbation": {},
            "clips": [
                {"source_path": "/a.mp4", "offset": 0, "duration_frames": 90},
                {"source_path": "/a.mp4", "offset": 0, "duration_frames": 90},
            ],
        }]
        with patch("autokat.core.slice_cache.build_cache_key", return_value="same"):
            result = prewarm_scripts(scripts, workers=2)
        self.assertEqual(cached.call_count, 1)
        self.assertEqual(result["references"], 2)
        self.assertEqual(result["unique_segments"], 1)
        self.assertEqual(result["expected_render_hit_rate"], 1.0)

    @patch("autokat.core.slice_cache.cached_segment")
    @patch("autokat.core.slice_cache.SliceCache.peek", return_value=None)
    def test_prewarm_prioritizes_hot_references_and_leaves_cold_on_demand(
        self, _peek, cached,
    ):
        cached.return_value = (Path("/tmp/cached.mp4"), "key")
        scripts = [{
            "fps": 30, "perturbation": {},
            "clips": [
                {"source_path": "/hot.mp4", "offset": 0, "duration_frames": 90},
                {"source_path": "/hot.mp4", "offset": 0, "duration_frames": 90},
                {"source_path": "/warm.mp4", "offset": 0, "duration_frames": 90,
                 "hotspot_score": 0.9},
                {"source_path": "/cold.mp4", "offset": 0, "duration_frames": 90,
                 "hotspot_score": 0.1},
            ],
        }]

        def key_for(path, *_args, **_kwargs):
            return path

        with patch("autokat.core.slice_cache.build_cache_key", side_effect=key_for):
            result = prewarm_scripts(
                scripts, workers=2, target_reference_coverage=0.70,
            )
        self.assertEqual(cached.call_count, 2)
        self.assertEqual(result["prewarm_segments"], 2)
        self.assertGreaterEqual(result["prewarm_reference_coverage"], 0.70)
        selected = {call.args[0]["source_path"] for call in cached.call_args_list}
        self.assertEqual(selected, {"/hot.mp4", "/warm.mp4"})

    @patch("autokat.core.slice_cache.run_ffmpeg")
    @patch("autokat.core.slice_cache._source_color_metadata")
    @patch("autokat.core.slice_cache.SliceCache.build")
    def test_hdr_cache_command_tonemaps_and_writes_bt709(
        self, build, metadata, run_ffmpeg,
    ):
        metadata.return_value = {
            "color_primaries": "bt2020",
            "color_space": "bt2020nc",
            "color_transfer": "arib-std-b67",
        }
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "hdr.mov"
            source.write_bytes(b"source")

            def invoke_builder(_key, builder):
                target = Path(tmp) / "cached.mp4"
                builder(target)
                return target

            build.side_effect = invoke_builder
            cached_segment(
                {"source_path": str(source), "duration_frames": 60},
                fps=30, render_frames=60,
            )
        command = run_ffmpeg.call_args.args[0]
        vf = command[command.index("-vf") + 1]
        self.assertIn("tonemap=mobius", vf)
        self.assertIn("zscale=p=bt709:t=bt709:m=bt709:r=tv", vf)
        self.assertIn("-an", command)
        self.assertEqual(command[command.index("-colorspace") + 1], "bt709")
        self.assertEqual(command[command.index("-color_primaries") + 1], "bt709")

    @patch("autokat.core.renderer.get_media_duration", return_value=0)
    @patch("autokat.core.renderer.run_ffmpeg")
    def test_composes_multiple_xfade_groups_in_one_encode(self, run_ffmpeg, _duration):
        files = [f"/tmp/{index}.mp4" for index in range(6)]
        durations = [2.0] * 6
        clips = [
            {"transition": "fade", "transition_frames": 9}
            for _ in files
        ]
        output, duration = _compose_cached_segments(
            files, durations, clips, 30, "/tmp",
        )
        self.assertEqual(output, "/tmp/composed.mp4")
        self.assertAlmostEqual(duration, 10.8)
        run_ffmpeg.assert_called_once()
        command = run_ffmpeg.call_args.args[0]
        graph = command[command.index("-filter_complex") + 1]
        self.assertEqual(graph.count("xfade="), 4)
        self.assertIn("concat=n=2:v=1:a=0[composed]", graph)

    @patch("autokat.core.renderer.get_supported_xfade_transitions", return_value=frozenset({"fade"}))
    @patch("autokat.core.renderer.get_media_duration", return_value=0)
    @patch("autokat.core.renderer.run_ffmpeg")
    def test_old_script_unsupported_transition_falls_back_to_fade(
        self, run_ffmpeg, _duration, _supported,
    ):
        _compose_cached_segments(
            ["/tmp/0.mp4", "/tmp/1.mp4"], [2.0, 2.0],
            [{"transition": "fade"}, {"transition": "revealleft", "transition_frames": 9}],
            30, "/tmp",
        )
        command = run_ffmpeg.call_args.args[0]
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("xfade=transition=fade:", graph)
        self.assertNotIn("revealleft", graph)

    @patch(
        "autokat.core.renderer.get_supported_xfade_transitions",
        return_value=frozenset({"fade", "revealleft"}),
    )
    @patch("autokat.core.renderer.get_media_duration", return_value=0)
    @patch("autokat.core.renderer.run_ffmpeg")
    def test_xfade_transition_runtime_error_retries_with_fade(
        self, run_ffmpeg, _duration, _supported,
    ):
        run_ffmpeg.side_effect = [
            subprocess.CalledProcessError(
                -11, ["ffmpeg"], stderr=b"Error applying option 'transition' to filter 'xfade': Option not found",
            ),
            None,
        ]
        _compose_cached_segments(
            ["/tmp/0.mp4", "/tmp/1.mp4"], [2.0, 2.0],
            [{"transition": "fade"}, {"transition": "revealleft", "transition_frames": 9}],
            30, "/tmp",
        )
        self.assertEqual(run_ffmpeg.call_count, 2)
        retry_command = run_ffmpeg.call_args.args[0]
        graph = retry_command[retry_command.index("-filter_complex") + 1]
        self.assertIn("xfade=transition=fade:", graph)
        self.assertNotIn("revealleft", graph)

    @patch("autokat.core.renderer.get_media_duration", return_value=0)
    @patch("autokat.core.renderer.run_ffmpeg")
    def test_spatial_perturbation_is_applied_once_after_composition(
        self, run_ffmpeg, _duration,
    ):
        _compose_cached_segments(
            ["/tmp/0.mp4", "/tmp/1.mp4"], [2.0, 2.0],
            [{"transition_frames": 9}, {"transition_frames": 9}],
            30, "/tmp",
            perturbation={"hflip": True, "scale": 1.02, "resolution": (1072, 1920)},
        )
        command = run_ffmpeg.call_args.args[0]
        graph = command[command.index("-filter_complex") + 1]
        self.assertEqual(graph.count("hflip"), 1)
        self.assertIn("[gx0_1]hflip,scale=", graph)
        self.assertIn("[perturbed]", graph)

    @patch("autokat.core.slice_cache.run_ffmpeg")
    @patch("autokat.core.slice_cache._source_color_metadata", return_value={})
    @patch("autokat.core.slice_cache.SliceCache.build")
    def test_base_cache_key_ignores_per_output_spatial_perturbation(
        self, build, _metadata, _run_ffmpeg,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.mp4"
            source.write_bytes(b"source")
            build.side_effect = lambda key, _builder: Path(tmp) / f"{key}.mp4"
            _, first = cached_segment(
                {"source_path": str(source), "duration_frames": 60}, 30,
                perturbation={"hflip": True, "scale": 1.02},
            )
            _, second = cached_segment(
                {"source_path": str(source), "duration_frames": 60}, 30,
                perturbation={"hflip": False, "scale": 0.96},
            )
        self.assertEqual(first, second)

    @patch("autokat.core.renderer.os.cpu_count", return_value=10)
    @patch("autokat.core.renderer.subprocess.run")
    def test_adaptive_workers_respect_cpu_memory_and_task_size(self, run, _cpu):
        run.return_value.stdout = str(32 * 1024 ** 3)
        workers, reason = _adaptive_worker_count(100)
        self.assertEqual(workers, 4)
        self.assertIn("任务 100 条", reason)

    @patch("autokat.core.renderer.get_video_duration", return_value=20.0)
    def test_retarget_frozen_clip_keeps_timeline_and_replaces_source(self, _duration):
        script = {"clips": [
            {
                "material_id": 1, "source_id": 1, "source_path": "/a.mp4",
                "source_type": "video", "offset": 5.0, "duration": 3.0,
                "start_time": 0.0, "end_time": 3.0, "cache_key": "old",
            },
            {
                "material_id": 2, "source_id": 2, "source_path": "/b.mp4",
                "source_type": "video", "offset": 2.0, "duration": 3.0,
                "start_time": 3.0, "end_time": 6.0,
            },
        ]}
        changed = _retarget_frozen_clips(script, [{
            "start": 1.0, "end": 2.5, "severity": "auto_fix",
            "reaches_tail": False,
        }])
        self.assertEqual(changed, 1)
        self.assertEqual(script["clips"][0]["source_path"], "/b.mp4")
        self.assertEqual(script["clips"][0]["start_time"], 0.0)
        self.assertEqual(script["clips"][0]["end_time"], 3.0)
        self.assertNotIn("cache_key", script["clips"][0])
