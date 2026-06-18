import inspect
import unittest

from autokat.ui.main_window import MainWindow


class AIProgressSummaryLayoutTests(unittest.TestCase):
    def test_tall_result_list_is_removed(self):
        source = inspect.getsource(MainWindow._on_wizard_ai_script)
        self.assertNotIn("_ai_results_list", source)
        self.assertNotIn("setMinimumHeight(180)", source)

    def test_latest_result_is_shown_below_progress(self):
        source = inspect.getsource(MainWindow._on_wizard_ai_script)
        self.assertIn("ai_latest_result_label", source)
        self.assertIn("最近生成：文案 #", source)
        self.assertIn("setMaximumHeight(24)", source)

    def test_all_generated_texts_are_still_used(self):
        source = inspect.getsource(MainWindow._on_wizard_ai_script)
        self.assertIn("dlg._ai_results = ai_results", source)
        self.assertIn('combined = "\\n---\\n".join(texts)', source)


if __name__ == "__main__":
    unittest.main()
