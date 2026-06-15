"""Regression test for v3.3: DeepSeek 空 choices 不再静默返回 None.

用户报告 (任务 follow-up):
「❌ DeepSeekWriterProvider 生成文案失败 (重试 3 次均不合格):
字数不足: 0 < 107；正文未围绕选题；空文案」

根因: _call_deepseek_api 用 result['choices'][0]['message']['content'] 直接
访问, 当模型名无效时 DeepSeek 返回 choices:[] 抛 IndexError, 被通用
except Exception 静默吞掉返回 None, 上游只看到「空文案」, 不知道真正原因。

修复: 改为先 check choices 是否为空, 空则 raise 带"模型名可能无效 +
官方仅支持 deepseek-chat / deepseek-reasoner"提示。
"""
import json
import unittest
from unittest.mock import patch

import autokat.core.writer as writer
from autokat.core.writer import _call_deepseek_api


class _FakeHTTPResponse:
    """Mimic urllib response: .read() returns bytes."""
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return json.dumps(self._payload, ensure_ascii=False).encode("utf-8")


class EmptyChoicesRaisesTests(unittest.TestCase):
    """v3.3: 模拟 DeepSeek API 返回空 choices 时, 必须 raise 明确错误"""

    def test_empty_choices_raises_with_model_name_hint(self):
        """DeepSeek 返回 {choices: []} (典型: 模型名无效) → raise 含模型名 + 官方支持列表"""
        fake_resp = _FakeHTTPResponse({"choices": [], "usage": {}})
        with patch.object(writer, "DEEPSEEK_API_KEY", "sk-test123"), \
             patch.object(writer, "DEEPSEEK_MODEL", "deepseek-v4-flash"), \
             patch("urllib.request.urlopen", return_value=fake_resp):
            with self.assertRaises(RuntimeError) as ctx:
                _call_deepseek_api("test prompt", max_tokens=50)
        msg = str(ctx.exception)
        self.assertIn("deepseek-v4-flash", msg,
                      f"异常必须含模型名, 实际: {msg!r}")
        self.assertIn("deepseek-chat", msg,
                      f"异常必须提示官方支持的模型, 实际: {msg!r}")
        self.assertIn("deepseek-reasoner", msg,
                      f"异常必须提示官方支持的模型, 实际: {msg!r}")

    def test_empty_message_content_raises(self):
        """DeepSeek 返回 choices 但 message.content 为空 → raise"""
        fake_resp = _FakeHTTPResponse({
            "choices": [{"message": {"content": ""}}],
            "usage": {},
        })
        with patch.object(writer, "DEEPSEEK_API_KEY", "sk-test123"), \
             patch.object(writer, "DEEPSEEK_MODEL", "deepseek-chat"), \
             patch("urllib.request.urlopen", return_value=fake_resp):
            with self.assertRaises(RuntimeError) as ctx:
                _call_deepseek_api("test prompt", max_tokens=50)
        msg = str(ctx.exception)
        self.assertIn("deepseek-chat", msg, f"异常必须含模型名, 实际: {msg!r}")
        self.assertIn("为空", msg, f"异常必须说明 content 为空, 实际: {msg!r}")

    def test_missing_choices_field_raises(self):
        """DeepSeek 返回 {} 完全无 choices 字段 → raise"""
        fake_resp = _FakeHTTPResponse({"error": "model not found"})
        with patch.object(writer, "DEEPSEEK_API_KEY", "sk-test123"), \
             patch.object(writer, "DEEPSEEK_MODEL", "deepseek-v4-flash"), \
             patch("urllib.request.urlopen", return_value=fake_resp):
            with self.assertRaises(RuntimeError) as ctx:
                _call_deepseek_api("test prompt", max_tokens=50)
        msg = str(ctx.exception)
        self.assertIn("deepseek-v4-flash", msg)

    def test_valid_response_returns_content(self):
        """正常响应 (choices[0].message.content 有内容) → 返回 strip 后字符串"""
        fake_resp = _FakeHTTPResponse({
            "choices": [{"message": {"content": "  这是一个好文案  "}}],
        })
        with patch.object(writer, "DEEPSEEK_API_KEY", "sk-test123"), \
             patch("urllib.request.urlopen", return_value=fake_resp):
            result = _call_deepseek_api("test", max_tokens=50)
        self.assertEqual(result, "这是一个好文案",
                          "正常响应应返回 strip 后的 content")

    def test_reasoning_model_falls_back_to_reasoning_content(self):
        """v3.3: 推理模型 (deepseek-v4-flash / deepseek-reasoner) content 经常为空,
        全部 token 被 reasoning_content 吃光。应该回退到 reasoning_content, 而
        不是 raise 空 content 错误。"""
        fake_resp = _FakeHTTPResponse({
            "choices": [{
                "message": {
                    "content": "",
                    "reasoning_content": "经过思考, 我认为这双时尚女鞋的核心卖点是...",
                    "role": "assistant",
                },
                "finish_reason": "length",
            }],
            "usage": {"completion_tokens": 15, "reasoning_tokens": 15},
        })
        with patch.object(writer, "DEEPSEEK_API_KEY", "sk-test123"), \
             patch.object(writer, "DEEPSEEK_MODEL", "deepseek-v4-flash"), \
             patch("urllib.request.urlopen", return_value=fake_resp):
            result = _call_deepseek_api("test", max_tokens=15)
        # 不应 raise, 应回退到 reasoning_content
        self.assertIn("时尚女鞋", result,
                       f"应回退到 reasoning_content, 实际: {result!r}")
        self.assertIn("经过思考", result)

    def test_generate_script_propagates_deepseek_error_with_model(self):
        """端到端: 用户报告的失败链路 (deepseek-v4-flash → 0 chars) 修复后
        raise 异常含 '模型名 + 手动录入' 提示, 而不是被吞成空文案"""
        def fake_call(*args, **kwargs):
            raise RuntimeError(
                "DeepSeek API 返回空 choices 字段 (模型 deepseek-v4-flash 可能无效"
                "或 Key 无权访问。DeepSeek 官方仅支持 deepseek-chat / deepseek-reasoner)。"
            )

        with patch.object(writer, "DEEPSEEK_API_KEY", "sk-test"), \
             patch("autokat.core.writer._call_deepseek_api", side_effect=fake_call):
            with self.assertRaises(RuntimeError) as ctx:
                writer.generate_script_by_topic_detailed(
                    "时尚女鞋", "种草推荐",
                    target_chars_min=100, target_chars_max=130,
                    provider="deepseek",
                )
        msg = str(ctx.exception)
        # v3.3 行为: 永久错误 (模型名错) 不再走 3 次重试, 立即向上抛。
        # 错误消息是 deepseek 层的具体诊断 (含模型名 + 官方支持列表),
        # 比「手动录入」更精准地告诉用户去哪里改。
        self.assertIn("deepseek-v4-flash", msg,
                      f"异常必须含具体模型名, 实际: {msg!r}")
        self.assertIn("deepseek-chat", msg,
                      f"异常必须提示官方支持列表, 实际: {msg!r}")
        self.assertIn("deepseek-reasoner", msg,
                      f"异常必须提示官方支持列表, 实际: {msg!r}")
        # 不应被吞成「字数 0 < 107 / 空文案」 (那是 v3.2 之前的错误现象)
        self.assertNotIn("字数不足", msg,
                          f"v3.3: 不应再被吞成空文案, 应直接 propagate 真实原因, 实际: {msg!r}")


if __name__ == "__main__":
    unittest.main()
