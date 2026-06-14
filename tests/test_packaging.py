import inspect
import unittest

import build_app


class PackagingTests(unittest.TestCase):
    def test_librosa_runtime_dependencies_are_bundled(self):
        source = inspect.getsource(build_app._embed_code)
        for package in (
            "librosa", "soundfile", "soxr", "numba", "llvmlite",
            "joblib", "msgpack", "sklearn", "aifc", "sunau", "chunk", "audioop",
        ):
            self.assertIn(f'"{package}"', source)

    def test_build_runs_packaged_pcm_vad_validation(self):
        build_source = inspect.getsource(build_app.build_app)
        validation_source = inspect.getsource(build_app._validate_embedded_runtime)
        self.assertIn("_validate_embedded_runtime", build_source)
        self.assertIn("detect_speech_intervals", validation_source)
        self.assertIn("NUMBA_CACHE_DIR", validation_source)
        self.assertIn("codesign", validation_source)

    def test_visual_model_and_onnx_runtime_are_bundled_and_validated(self):
        embed_source = inspect.getsource(build_app._embed_code)
        validation_source = inspect.getsource(build_app._validate_embedded_runtime)
        launcher_source = inspect.getsource(build_app._create_launcher)
        self.assertIn('"onnxruntime"', embed_source)
        self.assertIn("mobileclip_s0_image.onnx", embed_source)
        self.assertIn("mobileclip_s0_labels.npz", embed_source)
        self.assertIn("InferenceSession", validation_source)
        self.assertIn("(1, 512)", validation_source)
        self.assertIn("MobileCLIP", validation_source)
        self.assertIn("AUTOKAT_MODEL_DIR", launcher_source)


if __name__ == "__main__":
    unittest.main()
