"""Regression tests for save_deepseek_key error handling + file fallback.

User reported (task 176 follow-up):
  保存 DeepSeek API key 时报 exit 36
  (errSecInteractionNotAllowed) → 升级 macOS 15+ 后即使加 -A 仍然
  报 "SecKeychainItemModifyContent: User interaction is not allowed"

Root cause:
- macOS 15+ keychain 限制更严, -A 标志已不够
- 已有条目的 ACL 冲突 / locked keychain / sandbox 都会触发 User interaction
  not allowed
- 必须有文件兜底才能保证持久化一定成功

Fix:
- save_deepseek_key: macOS 先 keychain, 失败 → 文件兜底; 非 macOS 直接走文件
- load_deepseek_key: macOS 先 keychain, 为空 → 文件兜底; 非 macOS 直接走文件
- _KEY_FILE 用 chmod 600 (owner only) 原子写入
"""
import os
import stat
import subprocess
import sys
import unittest
from unittest.mock import patch, MagicMock

from autokat.core import ai_providers
from autokat.core.ai_providers import (
    KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT,
    _save_deepseek_key_keychain, _save_deepseek_key_file,
    _load_deepseek_key_file,
    save_deepseek_key, load_deepseek_key,
)


def _make_called_process_error(returncode, stderr=""):
    err = subprocess.CalledProcessError(returncode, ["security"])
    err.stderr = stderr
    return err


class KeychainSaveSuccessTests(unittest.TestCase):

    @patch("autokat.core.ai_providers.sys")
    @patch("autokat.core.ai_providers.subprocess.run")
    def test_keychain_save_success(self, mock_run, mock_sys):
        mock_sys.platform = "darwin"
        mock_run.return_value = MagicMock(returncode=0)
        result = _save_deepseek_key_keychain("sk-test123")
        self.assertTrue(result)
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        self.assertIn("-A", call_args,
                      "_save_deepseek_key_keychain 必须加 -A 标志")
        self.assertIn("-U", call_args)
        self.assertIn(KEYCHAIN_SERVICE, call_args)
        self.assertIn(KEYCHAIN_ACCOUNT, call_args)
        self.assertIn("sk-test123", call_args)


class KeychainSaveErrorTests(unittest.TestCase):

    @patch("autokat.core.ai_providers.sys")
    @patch("autokat.core.ai_providers.subprocess.run")
    def test_called_process_error_returns_false(self, mock_run, mock_sys):
        """macOS 15+ User interaction not allowed (exit 36) → keychain 内部返 False。"""
        mock_sys.platform = "darwin"
        mock_run.side_effect = _make_called_process_error(36, "interaction not allowed")
        self.assertFalse(_save_deepseek_key_keychain("sk-test123"))

    @patch("autokat.core.ai_providers.sys")
    @patch("autokat.core.ai_providers.subprocess.run")
    def test_other_nonzero_exit_returns_false(self, mock_run, mock_sys):
        mock_sys.platform = "darwin"
        mock_run.side_effect = _make_called_process_error(45, "duplicate item")
        self.assertFalse(_save_deepseek_key_keychain("sk-test123"))

    @patch("autokat.core.ai_providers.sys")
    @patch("autokat.core.ai_providers.subprocess.run")
    def test_file_not_found_returns_false(self, mock_run, mock_sys):
        mock_sys.platform = "darwin"
        mock_run.side_effect = FileNotFoundError("security: command not found")
        self.assertFalse(_save_deepseek_key_keychain("sk-test123"))


class FileSaveTests(unittest.TestCase):

    def setUp(self):
        # 每个测试用独立 tmp dir, 避免污染真实 _KEY_FILE
        self._tmp_key = ai_providers._KEY_FILE.parent / "_test_deepseek_key"
        self._patcher = patch.object(
            ai_providers, "_KEY_FILE", self._tmp_key,
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        if self._tmp_key.exists():
            self._tmp_key.unlink()

    def test_file_save_creates_file_with_chmod_600(self):
        ok = _save_deepseek_key_file("sk-file-test")
        self.assertTrue(ok)
        self.assertTrue(self._tmp_key.exists())
        # owner-only 权限 (S_IRUSR | S_IWUSR = 0o600)
        mode = stat.S_IMODE(self._tmp_key.stat().st_mode)
        self.assertEqual(mode, stat.S_IRUSR | stat.S_IWUSR,
                         f"文件权限必须是 0o600, 实际 {oct(mode)}")

    def test_file_save_writes_content(self):
        _save_deepseek_key_file("sk-content-test")
        self.assertEqual(
            self._tmp_key.read_text(encoding="utf-8"),
            "sk-content-test",
        )

    def test_file_save_overwrites_existing(self):
        _save_deepseek_key_file("sk-old")
        _save_deepseek_key_file("sk-new")
        self.assertEqual(
            self._tmp_key.read_text(encoding="utf-8"),
            "sk-new",
        )

    def test_file_load_returns_content(self):
        _save_deepseek_key_file("sk-load-test")
        self.assertEqual(_load_deepseek_key_file(), "sk-load-test")

    def test_file_load_strips_whitespace(self):
        _save_deepseek_key_file("  sk-strip-test  \n")
        self.assertEqual(_load_deepseek_key_file(), "sk-strip-test")

    def test_file_load_missing_returns_empty(self):
        if self._tmp_key.exists():
            self._tmp_key.unlink()
        self.assertEqual(_load_deepseek_key_file(), "")


class PublicSaveFallbackTests(unittest.TestCase):
    """save_deepseek_key 的对外行为: macOS 失败→文件, 非 macOS→文件直接"""

    def setUp(self):
        self._tmp_key = ai_providers._KEY_FILE.parent / "_test_public_save"
        self._patcher = patch.object(
            ai_providers, "_KEY_FILE", self._tmp_key,
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        if self._tmp_key.exists():
            self._tmp_key.unlink()

    @patch("autokat.core.ai_providers.sys")
    @patch("autokat.core.ai_providers._save_deepseek_key_keychain")
    def test_darwin_keychain_failure_falls_back_to_file(self, mock_keychain, mock_sys):
        """重现用户报告: macOS keychain 拒绝写入 → 自动降级文件, save 仍返 True"""
        mock_sys.platform = "darwin"
        mock_keychain.return_value = False  # keychain 失败
        ok = save_deepseek_key("sk-fallback-test")
        self.assertTrue(ok, "keychain 失败时 save_deepseek_key 必须返 True (有文件兜底)")
        mock_keychain.assert_called_once_with("sk-fallback-test")
        # 文件应该被写入
        self.assertTrue(self._tmp_key.exists())
        self.assertEqual(self._tmp_key.read_text(encoding="utf-8"), "sk-fallback-test")

    @patch("autokat.core.ai_providers.sys")
    @patch("autokat.core.ai_providers._save_deepseek_key_keychain")
    def test_darwin_keychain_success_skips_file(self, mock_keychain, mock_sys):
        mock_sys.platform = "darwin"
        mock_keychain.return_value = True
        ok = save_deepseek_key("sk-kc-only")
        self.assertTrue(ok)
        # keychain 成功时不应写文件
        self.assertFalse(self._tmp_key.exists())

    @patch("autokat.core.ai_providers.sys")
    @patch("autokat.core.ai_providers._save_deepseek_key_keychain")
    def test_non_darwin_skips_keychain(self, mock_keychain, mock_sys):
        mock_sys.platform = "linux"
        ok = save_deepseek_key("sk-linux")
        self.assertTrue(ok)
        mock_keychain.assert_not_called(), "非 macOS 不应尝试 keychain"
        self.assertTrue(self._tmp_key.exists())

    @patch("autokat.core.ai_providers.sys")
    @patch("autokat.core.ai_providers._save_deepseek_key_keychain")
    def test_windows_skips_keychain(self, mock_keychain, mock_sys):
        mock_sys.platform = "win32"
        ok = save_deepseek_key("sk-windows")
        self.assertTrue(ok)
        mock_keychain.assert_not_called()

    def test_empty_key_returns_false_no_side_effects(self):
        ok = save_deepseek_key("")
        self.assertFalse(ok)
        self.assertFalse(self._tmp_key.exists())


class PublicLoadFallbackTests(unittest.TestCase):
    """load_deepseek_key: keychain 优先, 文件兜底"""

    def setUp(self):
        self._tmp_key = ai_providers._KEY_FILE.parent / "_test_public_load"
        self._patcher = patch.object(
            ai_providers, "_KEY_FILE", self._tmp_key,
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        if self._tmp_key.exists():
            self._tmp_key.unlink()

    @patch("autokat.core.ai_providers.sys")
    @patch("autokat.core.ai_providers.subprocess.run")
    def test_darwin_loads_from_keychain_first(self, mock_run, mock_sys):
        mock_sys.platform = "darwin"
        mock_run.return_value = MagicMock(stdout="sk-from-keychain\n")
        # 文件也在, 但 keychain 应优先
        _save_deepseek_key_file("sk-from-file")
        self.assertEqual(load_deepseek_key(), "sk-from-keychain")

    @patch("autokat.core.ai_providers.sys")
    @patch("autokat.core.ai_providers.subprocess.run")
    def test_darwin_falls_back_to_file_when_keychain_empty(self, mock_run, mock_sys):
        """用户实际场景: keychain 写入失败 → 文件成功 → 启动时读文件"""
        mock_sys.platform = "darwin"
        mock_run.side_effect = _make_called_process_error(36, "interaction not allowed")
        _save_deepseek_key_file("sk-from-file-fallback")
        self.assertEqual(load_deepseek_key(), "sk-from-file-fallback")

    @patch("autokat.core.ai_providers.sys")
    @patch("autokat.core.ai_providers.subprocess.run")
    def test_non_darwin_loads_from_file(self, mock_run, mock_sys):
        mock_sys.platform = "linux"
        _save_deepseek_key_file("sk-linux-key")
        self.assertEqual(load_deepseek_key(), "sk-linux-key")
        mock_run.assert_not_called()

    @patch("autokat.core.ai_providers.sys")
    @patch("autokat.core.ai_providers.subprocess.run")
    def test_returns_empty_when_nothing_anywhere(self, mock_run, mock_sys):
        mock_sys.platform = "darwin"
        mock_run.side_effect = _make_called_process_error(44, "item not found")
        if self._tmp_key.exists():
            self._tmp_key.unlink()
        self.assertEqual(load_deepseek_key(), "")


class MigrateWithBoolReturnTests(unittest.TestCase):
    """migrate_env_key_to_keychain 应兼容新 bool 返回签名"""

    def test_migrate_with_save_returning_true(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "old-key"}), \
             patch.object(ai_providers, "save_deepseek_key", return_value=True), \
             patch.object(ai_providers, "load_deepseek_key", return_value=""):
            migrated = ai_providers.migrate_env_key_to_keychain()
        self.assertTrue(migrated)
        self.assertNotIn("DEEPSEEK_API_KEY", os.environ)


if __name__ == "__main__":
    unittest.main()
