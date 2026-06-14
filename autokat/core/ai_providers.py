"""Explicit AI writer providers and DeepSeek configuration."""

from __future__ import annotations

import json
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from autokat.core.paths import DATA_ROOT


SETTINGS_PATH = DATA_ROOT / "config" / "ai_settings.json"
KEYCHAIN_SERVICE = "com.autokat.deepseek"
KEYCHAIN_ACCOUNT = "api-key"


def load_ai_settings() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {
            "provider": "local",
            "deepseek_url": "https://api.deepseek.com/v1/chat/completions",
            "deepseek_model": "deepseek-chat",
        }


def save_ai_settings(settings: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def save_deepseek_key(api_key: str) -> None:
    subprocess.run(
        ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE,
         "-a", KEYCHAIN_ACCOUNT, "-w", api_key],
        check=True, capture_output=True, text=True,
    )


def load_deepseek_key() -> str:
    try:
        return subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
             "-a", KEYCHAIN_ACCOUNT, "-w"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except Exception:
        return ""


def migrate_env_key_to_keychain(env_var: str = "DEEPSEEK_API_KEY") -> bool:
    """Move a legacy .env style secret into the macOS Keychain (idempotent).

    Returns True when a migration actually happened. After migrating once the
    runtime no longer relies on the environment variable, so the caller should
    delete the env entry to avoid confusion.
    """
    import os

    api_key = (os.environ.get(env_var) or "").strip()
    if not api_key:
        return False
    current = load_deepseek_key()
    if current == api_key:
        os.environ.pop(env_var, None)
        return False
    try:
        save_deepseek_key(api_key)
    except Exception:
        return False
    os.environ.pop(env_var, None)
    return True


VIDEO_TYPE_PROMPTS: dict[str, str] = {
    "product_recommendation": (
        "视频类型：商品推荐。围绕商品的卖点、搭配、使用场景组织内容，"
        "重点突出实用价值和情绪收益，避免堆叠促销词。"
    ),
    "talking_explanation": (
        "视频类型：口播讲解。围绕知识点、步骤、原因逐条讲解，"
        "结构清晰，逻辑递进，避免华丽辞藻。"
    ),
    "atmosphere": (
        "视频类型：氛围记录。用画面感和场景描写制造氛围，"
        "情绪为主，产品为辅，避免硬广口吻。"
    ),
    "music_beat": (
        "视频类型：音乐卡点。文案节奏与画面切分配合 BGM 节拍，"
        "短句、留白、有呼吸感，不要长篇论述。"
    ),
    "random_mix": (
        "视频类型：随机混剪。围绕主题自由组织情绪和场景，"
        "不做结构化讲解，每条文案换一种叙述角度。"
    ),
    "auto": (
        "视频类型：由你根据选题自由判断，按最贴合主题的方式组织内容。"
    ),
}


def video_type_prompt_hint(video_type) -> str:
    key = (str(video_type) if video_type else "auto").strip()
    return VIDEO_TYPE_PROMPTS.get(key, VIDEO_TYPE_PROMPTS["auto"])


def build_writer_provider(name: str):
    """Single factory for all writer providers.

    - ``local`` returns :class:`LocalWriterProvider`.
    - ``deepseek`` loads the key from macOS Keychain and the URL/model from
      ``ai_settings.json``. Missing key raises ``RuntimeError`` and never
      silently switches to the local provider.
    - any other name raises ``ValueError``.
    """
    key = (name or "local").strip().lower()
    if key == "local":
        return LocalWriterProvider()
    if key == "deepseek":
        cfg = load_ai_settings()
        api_key = load_deepseek_key()
        if not api_key:
            raise RuntimeError("已选择 DeepSeek，但尚未配置有效 API Key")
        return DeepSeekWriterProvider(
            api_key,
            cfg.get("deepseek_url", "https://api.deepseek.com/v1/chat/completions"),
            cfg.get("deepseek_model", "deepseek-chat"),
        )
    raise ValueError(f"不支持的文案模型: {name}")
@dataclass
class DeepSeekWriterProvider:
    api_key: str
    api_url: str
    model: str

    def generate(self, prompt: str, max_tokens: int = 512) -> str:
        from autokat.core.writer import _call_deepseek_api
        result = _call_deepseek_api(
            prompt, max_tokens=max_tokens,
            api_key=self.api_key, api_url=self.api_url, model=self.model,
        )
        if not result:
            raise RuntimeError("DeepSeek 调用未返回有效正文")
        return result

    def test_connection(self) -> dict:
        if not self.api_key.strip():
            raise ValueError("API Key 为空")
        if not self.api_url.startswith(("https://", "http://")):
            raise ValueError("API 地址必须是 http/https URL")
        if not self.model.strip():
            raise ValueError("模型名称为空")
        content = self.generate("只回复 OK", max_tokens=16)
        return {"valid": True, "model": self.model, "response": content[:80]}


class LocalWriterProvider:
    def generate(self, prompt: str, max_tokens: int = 512) -> str:
        from autokat.core.writer import _call_local_model
        result = _call_local_model(prompt, max_length=max_tokens)
        if not result:
            raise RuntimeError("本地模型未返回有效正文")
        return result
