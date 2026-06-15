"""Regression test for "cannot access free variable 'Qt' ..." crash on AI 辅助文案按钮.

Root cause:
  _on_wizard_ai_script 函数体里有 `from PySide6.QtCore import Qt`,
  Python 编译器因此把 Qt 当作 cellvar (co_cellvars)。函数体内
  定义的 nested function `_make_help_label` 在 line 1691 引用
  `Qt.PointingHandCursor`, Python 编译器把 Qt 当作 freevar 期望
  从 outer cell 读。但 `_make_help_label` 在 line 1710 被调用时,
  那个 `import Qt` 还没执行 (它在 offset 2188 才 STORE_DEREF Qt),
  于是 cell 还是空, 抛:
    NameError: cannot access free variable 'Qt' where it is not
    associated with a value in enclosing scope

修复:
  删掉函数体内冗余的 `from PySide6.QtCore import Qt` 和
  `from PySide6.QtWidgets import QSlider` — 顶部第 6、8 行已 import 过。
  这让 Qt 走模块全局, nested function 正常 LOAD_GLOBAL。
"""
import ast
import inspect
import unittest

from PySide6.QtWidgets import QApplication, QDialog


class QtClosureRegressionTests(unittest.TestCase):
    """守护 _on_wizard_ai_script 不再埋这个雷"""

    @classmethod
    def setUpClass(cls):
        # 需要 QApplication 才能实例化 MainWindow
        cls._app = QApplication.instance() or QApplication([])

    def test_ai_script_qt_not_in_cellvars(self):
        """字节码层面: Qt 不能是 _on_wizard_ai_script 的 cellvar (否则埋雷)"""
        from autokat.ui.main_window import MainWindow
        fn = MainWindow._on_wizard_ai_script
        cellvars = fn.__code__.co_cellvars or ()
        self.assertNotIn(
            "Qt", cellvars,
            f"_on_wizard_ai_script 把 Qt 当 cellvar 会导致 nested function "
            f"`_make_help_label` 在 cell 未初始化时被调用, 抛 "
            f"'cannot access free variable Qt'。请删掉函数体内的 "
            f"`from PySide6.QtCore import Qt` (顶部已 import 过)。",
        )

    def test_ai_script_runs_without_crash(self):
        """运行时层面: 调用 _on_wizard_ai_script 不应抛 NameError"""
        from autokat.ui.main_window import MainWindow

        # 让 modal dialog 立即返回, 函数能跑完
        original_exec = QDialog.exec
        QDialog.exec = lambda self: 0
        try:
            w = MainWindow()
            try:
                w._on_wizard_ai_script()
            except NameError as e:
                if "Qt" in str(e) and "free variable" in str(e):
                    self.fail(
                        f"点 AI 辅助文案按钮仍然报 Qt free variable 错误: {e}"
                    )
                raise
        finally:
            QDialog.exec = original_exec


class NoNestedQtClosureInFunctionBodyTests(unittest.TestCase):
    """通用守卫: 扫描 main_window.py, 任何「函数体内 import Qt」+「嵌套函数引用 Qt」的
    组合都应被禁掉。这是 Python 闭包陷阱的高发场景。"""

    SUSPECT_MODULES = ("autokat.ui.main_window",)

    def test_no_inner_function_imports_qt_with_nested_qt_ref(self):
        import importlib
        bad = []
        for mod_name in self.SUSPECT_MODULES:
            mod = importlib.import_module(mod_name)
            for name, fn in inspect.getmembers(mod, predicate=inspect.isfunction):
                if not hasattr(fn, "__code__"):
                    continue
                try:
                    src = inspect.getsource(fn)
                except (OSError, TypeError):
                    continue
                try:
                    tree = ast.parse(src)
                except SyntaxError:
                    continue
                func_node = tree.body[0]
                # 1. 函数体里有 `from PySide6.QtCore import ..., Qt, ...` 吗?
                imports_qt_locally = False
                for node in ast.walk(func_node):
                    if (isinstance(node, ast.ImportFrom)
                            and node.module == "PySide6.QtCore"):
                        if any(a.name == "Qt" for a in node.names):
                            imports_qt_locally = True
                            break
                if not imports_qt_locally:
                    continue
                # 2. 函数内有嵌套函数引用 Qt 吗?
                nested_ref_qt = False
                for node in ast.walk(func_node):
                    if (isinstance(node, ast.FunctionDef)
                            and node is not func_node):
                        for sub in ast.walk(node):
                            if isinstance(sub, ast.Name) and sub.id == "Qt":
                                nested_ref_qt = True
                                break
                        if nested_ref_qt:
                            break
                if nested_ref_qt:
                    bad.append(f"{mod_name}.{name}")

        self.assertEqual(
            bad, [],
            f"以下函数有「函数体内 import Qt + 嵌套函数引用 Qt」组合, "
            f"会触发 'cannot access free variable Qt' NameError:\n  "
            + "\n  ".join(bad)
            + "\n\n修复: 删掉函数体内的 `from PySide6.QtCore import Qt`, "
            "模块顶部已 import, 直接用即可。",
        )


if __name__ == "__main__":
    unittest.main()
