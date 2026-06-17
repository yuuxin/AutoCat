"""v3.22 守护测试: 素材视觉分析抽帧前校验文件存在."""
import unittest
from unittest.mock import patch, MagicMock

from autokat.core.material_analysis import _representative_image


class RepresentativeImageFileCheckTests(unittest.TestCase):
    def _make_material(self, file_path, mat_type="video", duration=5.0):
        return {
            "id": 9999,
            "file_path": file_path,
            "mat_type": mat_type,
            "duration": duration,
        }

    def test_nonexistent_path_raises_filenotfound(self):
        with patch("autokat.core.material_analysis.run_ffmpeg") as mock_ffmpeg:
            with self.assertRaises(FileNotFoundError) as ctx:
                _representative_image(self._make_material("/tmp/no_such_file_v322.mp4"))
            msg = str(ctx.exception)
            self.assertIn("9999", msg)
            self.assertIn("/tmp/no_such_file_v322.mp4", msg)
            self.assertIn("DB", msg)
            mock_ffmpeg.assert_not_called()

    def test_user_reported_bad_paths_all_caught(self):
        bad_paths = [
            "/tmp/empty.mp4",
            "/tmp/shoe0.mp4",
            "/tmp/no_such_file.mp4",
            "/var/folders/abc/T/tmp123/no_such_file.mp4",
            "/tmp/女鞋特写.mp4",
        ]
        for path in bad_paths:
            with self.subTest(path=path):
                with patch("autokat.core.material_analysis.run_ffmpeg") as mock_ffmpeg:
                    with self.assertRaises(FileNotFoundError):
                        _representative_image(self._make_material(path))
                    mock_ffmpeg.assert_not_called()

    def test_directory_path_raises(self):
        with patch("autokat.core.material_analysis.Path") as mock_path_cls:
            mock_source = MagicMock()
            mock_source.exists.return_value = True
            mock_source.is_file.return_value = False
            mock_path_cls.return_value = mock_source
            with patch("autokat.core.material_analysis.run_ffmpeg") as mock_ffmpeg:
                with self.assertRaises(FileNotFoundError):
                    _representative_image(self._make_material("/fake/dir"))
                mock_ffmpeg.assert_not_called()



if __name__ == "__main__":
    unittest.main()
