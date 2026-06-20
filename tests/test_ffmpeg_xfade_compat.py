import subprocess
import unittest
from unittest.mock import patch

from autokat.core.ffmpeg_utils import _probe_xfade_transitions


class XfadeCompatibilityTests(unittest.TestCase):
    def tearDown(self):
        _probe_xfade_transitions.cache_clear()

    @patch("autokat.core.ffmpeg_utils.subprocess.run")
    def test_probe_reads_only_transitions_advertised_by_ffmpeg(self, run):
        run.return_value = subprocess.CompletedProcess(
            [], 0, stdout="", stderr="""
xfade AVOptions:
   transition        <int>        set cross fade transition
     custom          -1           custom transition
     fade            0            fade transition
     fadeslow        45           slow fade transition
   duration          <duration>   set cross fade duration
""",
        )
        self.assertEqual(
            _probe_xfade_transitions("/tmp/ffmpeg"),
            frozenset({"fade", "fadeslow"}),
        )

    @patch("autokat.core.ffmpeg_utils.subprocess.run", side_effect=OSError("missing"))
    def test_probe_failure_uses_safe_fade_only_fallback(self, _run):
        self.assertEqual(
            _probe_xfade_transitions("/tmp/missing-ffmpeg"),
            frozenset({"fade"}),
        )


if __name__ == "__main__":
    unittest.main()
