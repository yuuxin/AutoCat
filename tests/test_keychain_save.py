"""Regression tests for save_deepseek_key error handling.

User reported (task 176 follow-up):
  保存 DeepSeek API key 时报 exit 36 (errSecInteractionNotAllowed):
  Command '['security', 'add-generic-password', ...]' returned non-zero exit status 36.

Root cause:
- macOS 15+ 对未签名脚本访问 keychain 限制更严, security 工具即使加了 -U
  也可能因为 interaction not allowed 而失败
- 之前的 save_deepseek_key 用 check=True 直接抛 CalledProcessError,
  UI 直接收到 raw stacktrace, 用户看不到任何可操作的提示

Fix expectations:
1. save_deepseek_key 返回 bool (True=成功, False=失败), 不再 raise
2. macOS 15+ 加 -A 标志 (allow all applications), 提高成功率
3. 失败时打印友好消息, 说明 key 仍可用于本次会话
4. 非 macOS 平台直接返回 False, 不调用 subprocess
5. UI _save() 处理 False 时, status_label 显示警告并保留对话框
"""
import os
import subprocess
import sys
import unittest
from unittest.mock import patch, MagicMock

from autokat.core import ai_providers
from autokat.core.ai_providers import (
    KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT, save_deepseek_key,
)


def _make_called_process_error(returncode, stderr=""):
    """构造一个与 subprocess.run(check=True) 抛出的异常等价的对象。"""
    err = subprocess.CalledProcessError(returncode, ["security"])
    err.stderr = stderr
    return err


class SaveDeepseekKeySuccessTests(unittest.TestCase):

    @patch("autokat.core.ai_providers.sys")
    @patch("autokat.core.ai_providers.subprocess.run")
    def test_save_success_returns_true(self, mock_run, mock_sys):
        mock_sys.platform = "darwin"
        mock_run.return_value = MagicMock(returncode=0)
        result = save_deepseek_key("sk-test123")
        self.assertTrue(result)
        mock_run.assert_called_once()
        # 验证调用参数包含 -A 标志 (allow-all-applications)
        call_args = mock_run.call_args[0][0]
        self.assertIn("-A", call_args,
                      "save_deepseek_key 必须加 -A 标志 (macOS 15+ 需要)")
        self.assertIn("-U", call_args)
        self.assertIn(KEYCHAIN_SERVICE, call_args)
        self.assertIn(KEYCHAIN_ACCOUNT, call_args)
        self.assertIn("sk-test123", call_args)


class SaveDeepseekKeyErrorTests(unittest.TestCase):

    @patch("autokat.core.ai_providers.sys")
    @patch("autokat.core.ai_providers.subprocess.run")
    def test_called_process_error_returns_false(self, mock_run, mock_sys):
        """重现用户报告的 exit 36 场景 → 不应抛异常, 应返回 False。"""
        mock_sys.platform = "darwin"
        mock_run.side_effect = _make_called_process_error(36, "interaction not allowed")
        result = save_deepseek_key("sk-test123")
        self.assertFalse(result,
                         "exit 36 必须返回 False 而不是抛 CalledProcessError")

    @patch("autokat.core.ai_providers.sys")
    @patch("autokat.core.ai_providers.subprocess.run")
    def test_other_nonzero_exit_returns_false(self, mock_run, mock_sys):
        mock_sys.platform = "darwin"
        mock_run.side_effect = _make_called_process_error(45, "duplicate item")
        self.assertFalse(save_deepseek_key("sk-test123"))

    @patch("autokat.core.ai_providers.sys")
    @patch("autokat.core.ai_providers.subprocess.run")
    def test_file_not_found_returns_false(self, mock_run, mock_sys):
        """非 macOS 但 sys.platform 被 patch 成 darwin, security 不存在的情况。"""
        mock_sys.platform = "darwin"
        mock_run.side_effect = FileNotFoundError("security: command not found")
        self.assertFalse(save_deepseek_key("sk-test123"))


class SaveDeepseekKeyPlatformTests(unittest.TestCase):

    @patch("autokat.core.ai_providers.sys")
    @patch("autokat.core.ai_providers.subprocess.run")
    def test_non_darwin_skips_subprocess(self, mock_run, mock_sys):
        mock_sys.platform = "linux"
        result = save_deepseek_key("sk-test123")
        self.assertFalse(result)
        self.assertEqual(mock_run.call_count, 0,
                         "非 macOS 平台不应调用 subprocess")

    @patch("autokat.core.ai_providers.sys")
    @patch("autokat.core.ai_providers.subprocess.run")
    def test_windows_skips_subprocess(self, mock_run, mock_sys):
        mock_sys.platform = "win32"
        self.assertFalse(save_deepseek_key("sk-test123"))
        mock_run.assert_not_called()


class SaveDeepseekKeyEdgeCaseTests(unittest.TestCase):

    def test_empty_key_returns_false_without_subprocess(self):
        """空 key 直接 False, 不调用任何外部命令。"""
        with patch("autokat.core.ai_providers.subprocess.run") as mock_run:
            result = save_deepseek_key("")
            self.assertFalse(result)
            mock_run.assert_not_called()


class MigrateWithBoolReturnTests(unittest.TestCase):
    """migrate_env_key_to_keychain 应兼容新的 bool 返回签名。"""

    def test_migrate_with_save_returning_false(self):
        """新签名下 save 返回 False 时, migrate 仍返回 True (env 已 pop)。"""
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "old-key"}), \
             patch.object(ai_providers, "save_deepseek_key", return_value=False), \
             patch.object(ai_providers, "load_deepseek_key", return_value=""):
            migrated = ai_providers.migrate_env_key_to_keychain()
        # env var 已 pop, migrate 视为已完成
        self.assertTrue(migrated)
        self.assertNotIn("DEEPSEEK_API_KEY", os.environ)

    def test_migrate_with_save_returning_true(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "old-key"}), \
             patch.object(ai_providers, "save_deepseek_key", return_value=True), \
             patch.object(ai_providers, "load_deepseek_key", return_value=""):
            migrated = ai_providers.migrate_env_key_to_keychain()
        self.assertTrue(migrated)
        self.assertNotIn("DEEPSEEK_API_KEY", os.environ)


if __name__ == "__main__":
    unittest.main()
