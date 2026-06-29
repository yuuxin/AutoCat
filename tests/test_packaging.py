import unittest
from pathlib import Path


class PackagingTests(unittest.TestCase):
    def test_pyinstaller_spec_bundles_models_and_native_packages(self):
        source = Path("packaging/AutoCat.spec").read_text(encoding="utf-8")
        self.assertIn("mobileclip_s0_image.onnx", source)
        self.assertIn("mobileclip_s0_labels.npz", source)
        self.assertIn('collect_submodules("edge_tts")', source)
        self.assertNotIn("collect_all", source)
        self.assertIn("runtime_hook.py", source)
        self.assertNotIn("assets/clips", source)
        self.assertIn("AUTOKAT_LOCAL_MODEL_MODE", source)
        self.assertIn('"download"', source)

    def test_packaged_smoke_test_covers_required_runtime(self):
        source = Path("autokat/__main__.py").read_text(encoding="utf-8")
        for marker in (
            "--packaged-smoke-test",
            "sqlite3.connect",
            "InferenceSession",
            "CPUExecutionProvider",
            "libx264",
            "qVersion",
            "_call_local_model",
            "local_model_mode",
            "local_model_response",
        ):
            self.assertIn(marker, source)

    def test_runtime_hook_isolates_user_environment(self):
        source = Path("packaging/runtime_hook.py").read_text(encoding="utf-8")
        for variable in (
            "PYTHONHOME",
            "PYTHONPATH",
            "VIRTUAL_ENV",
            "CONDA_PREFIX",
            "DYLD_LIBRARY_PATH",
            "DYLD_FRAMEWORK_PATH",
        ):
            self.assertIn(variable, source)
        self.assertIn("AUTOKAT_TOOLS_DIR", source)
        self.assertIn("AUTOKAT_LOCAL_MODEL_DIR", source)
        self.assertIn("TRANSFORMERS_OFFLINE", source)
        self.assertIn("HF_HOME", source)
        self.assertIn('"download"', source)

    def test_windows_workflow_uploads_verified_distributables(self):
        source = Path(".github/workflows/build.yml").read_text(encoding="utf-8")
        for marker in (
            "Build installer",
            "Build portable zip",
            "Verify distributables",
            "Expand-Archive -Path $zipPath",
            "AutoCat.exe",
            "ffmpeg.exe",
            "dist/SHA256SUMS",
            "compression-level: 0",
            "windows-x86_64-portable.zip",
        ):
            self.assertIn(marker, source)
        self.assertNotIn("path: dist/AutoCat/", source)

    def test_windows_local_build_script_creates_portable_outputs(self):
        source = Path("build_win.py").read_text(encoding="utf-8")
        for marker in (
            "build_portable_zip",
            "allowZip64=True",
            "AutoCat.exe",
            "ffmpeg.exe",
            "testzip",
            "write_sha256sums",
            "SHA256SUMS",
            "windows-x86_64-portable.zip",
        ):
            self.assertIn(marker, source)


if __name__ == "__main__":
    unittest.main()
