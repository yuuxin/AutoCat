"""Regression tests for DeepSeek API HTTP error handling (任务 239 follow-up).

用户报告: DeepSeek API 调用失败 HTTP Error 401: Authorization Required
所有重试都报 401, UI 看不到原因也不知道下一步该做什么。

根因: _call_deepseek_api 用 urllib.request + 笼统的 except Exception
捕获错误, 把 HTTPError 跟其他错误一样吞掉, print 一行用户看不懂的
stacktrace, 然后返回 None。上游 DeepSeekWriterProvider.generate 看到 None
就 raise "DeepSeek 调用未返回有效正文" — 用户还是不知道是 Key 错还是
网络问题。

修复:
- 针对 401/402/429 等常见 HTTP 状态码给出中文可操作提示
- raise RuntimeError 而不是 return None, 让异常路径直达 UI 日志
- 响应体也尽量截下来供诊断
"""
import json
import unittest
from unittest.mock import patch, MagicMock

import autokat.core.writer as writer


class DeepSeek401HandlingTests(unittest.TestCase):

    def _patch_urlopen(self, mock_urlopen, *, status, body=b'{"error": "auth"}'):
        """把 urllib.request.urlopen mock 成抛 HTTPError (status, body)"""
        import urllib.error
        err = urllib.error.HTTPError(
            url="https://api.deepseek.com/v1/chat/completions",
            code=status,
            msg="Unauthorized" if status == 401 else "Error",
            hdrs={},
            fp=MagicMock(read=lambda: body),
        )
        mock_urlopen.side_effect = err

    @patch("urllib.request.urlopen")
    def test_401_raises_with_actionable_message(self, mock_urlopen):
        """401 必须 raise 带「Key 认证失败」+ 去平台查 Key 的提示"""
        self._patch_urlopen(mock_urlopen, status=401)
        with self.assertRaises(RuntimeError) as ctx:
            writer._call_deepseek_api("hello", api_key="sk-test")
        msg = str(ctx.exception)
        self.assertIn("401", msg)
        self.assertIn("platform.deepseek.com", msg,
                       "401 错误必须提示用户去 DeepSeek 平台检查 Key")
        self.assertIn("过期", msg)
        self.assertIn("重新填入", msg)

    @patch("urllib.request.urlopen")
    def test_402_balance_raises_with_payment_hint(self, mock_urlopen):
        """402 必须 raise 带「余额不足」+ 充值提示"""
        self._patch_urlopen(mock_urlopen, status=402)
        with self.assertRaises(RuntimeError) as ctx:
            writer._call_deepseek_api("hello", api_key="sk-test")
        self.assertIn("402", str(ctx.exception))
        self.assertIn("余额不足", str(ctx.exception))
        self.assertIn("充值", str(ctx.exception))

    @patch("urllib.request.urlopen")
    def test_429_rate_limit_raises_with_wait_hint(self, mock_urlopen):
        """429 必须 raise 带「频率超限」+ 等候/减小并发提示"""
        self._patch_urlopen(mock_urlopen, status=429)
        with self.assertRaises(RuntimeError) as ctx:
            writer._call_deepseek_api("hello", api_key="sk-test")
        self.assertIn("429", str(ctx.exception))
        self.assertIn("频率", str(ctx.exception))
        self.assertIn("并发", str(ctx.exception))

    @patch("urllib.request.urlopen")
    def test_500_unknown_returns_response_body(self, mock_urlopen):
        """500 等未知错误要把响应体截下来供诊断, 不至于一片空白"""
        body = b'{"error": "internal server error"}'
        self._patch_urlopen(mock_urlopen, status=500, body=body)
        with self.assertRaises(RuntimeError) as ctx:
            writer._call_deepseek_api("hello", api_key="sk-test")
        self.assertIn("500", str(ctx.exception))
        self.assertIn("internal server error", str(ctx.exception))

    @patch("urllib.request.urlopen")
    def test_successful_call_still_returns_content(self, mock_urlopen):
        """正常 200 响应必须照旧返回 content, 不被新逻辑误伤"""
        # mock_urlopen 要当 context manager 用 (`with urlopen(...) as resp:`)
        # 默认 MagicMock 的 __enter__ 返回一个全新的 MagicMock, 不是 mock_resp,
        # 所以 resp.read() 又拿到 MagicMock。把 __enter__.return_value 指向自己。
        resp = MagicMock()
        resp.read = MagicMock(return_value=json.dumps({
            "choices": [{"message": {"content": "  hello world  "}}],
        }).encode("utf-8"))
        mock_urlopen.return_value.__enter__.return_value = resp
        mock_urlopen.return_value.__exit__.return_value = False
        result = writer._call_deepseek_api("test", api_key="sk-test")
        self.assertEqual(result, "hello world")

    @patch("urllib.request.urlopen")
    def test_empty_key_returns_none_not_raises(self, mock_urlopen):
        """空 key 时 _call_deepseek_api 仍然返回 None (上游会兜底),
        不要抛 401 (空 key 不是 401)。"""
        # 没设置 DEEPSEEK_API_KEY, 显式传空字符串
        result = writer._call_deepseek_api("test", api_key="")
        self.assertIsNone(result)
        mock_urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
