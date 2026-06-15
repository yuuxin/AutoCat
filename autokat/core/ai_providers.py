"""Explicit AI writer providers and DeepSeek configuration."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from autokat.core.paths import DATA_ROOT


SETTINGS_PATH = DATA_ROOT / "config" / "ai_settings.json"
KEYCHAIN_SERVICE = "com.autokat.deepseek"
KEYCHAIN_ACCOUNT = "api-key"
# v3.2: 文件存储兜底 — macOS keychain 在某些上下文 (locked keychain / sandbox /
# 已有条目 ACL 冲突) 即使有 -A 也会拒绝写入, 必须有 fallback。
# 选 chmod 600 的单独文件, 不与 ai_settings.json 共享目录, 避免权限继承问题。
_KEY_FILE = DATA_ROOT / "config" / "deepseek_key"


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


def _save_deepseek_key_keychain(api_key: str) -> bool:
    """纯 keychain 写入路径。无外部依赖副作用,返回 True/False。"""
    try:
        subprocess.run(
            ["security", "add-generic-password", "-A", "-U", "-s", KEYCHAIN_SERVICE,
             "-a", KEYCHAIN_ACCOUNT, "-w", api_key],
            check=True, capture_output=True, text=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip() or f"exit {e.returncode}"
        print(
            f"[deepseek] ⚠️  Keychain 保存失败 ({stderr})。"
            f"将自动降级到 chmod 600 文件存储。"
        )
        return False
    except FileNotFoundError:
        print("[deepseek] ⚠️  security 命令不可用, keychain 不可用")
        return False


def _save_deepseek_key_file(api_key: str) -> bool:
    """文件存储兜底。chmod 600 原子写入,跨平台可用。

    原因: macOS keychain 在某些上下文 (locked keychain / sandbox /
    已有条目 ACL 冲突) 即使有 -A 也会拒绝写入 (返回
    SecKeychainItemModifyContent: User interaction is not allowed) —
    只有文件存储才能保证一定能持久化。文件与 ai_settings.json 同目录
    但单独文件,便于单独设权限 0o600 (owner only)。
    """
    try:
        _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _KEY_FILE.with_suffix(_KEY_FILE.suffix + ".tmp")
        tmp.write_text(api_key, encoding="utf-8")
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, _KEY_FILE)  # POSIX rename 原子
        return True
    except OSError as e:
        print(f"[deepseek] ⚠️  文件兜底保存失败: {e}")
        return False


def save_deepseek_key(api_key: str) -> bool:
    """保存 DeepSeek API key。先 keychain,失败兜底到 chmod 600 文件。

    macOS 15+ keychain 经常返回 User interaction is not allowed
    (即使 -A 也不够) — 此时降级到文件存储,保证持久化一定成功。
    非 macOS 平台直接走文件路径。
    """
    if not api_key:
        return False
    if sys.platform == "darwin":
        if _save_deepseek_key_keychain(api_key):
            return True
    return _save_deepseek_key_file(api_key)


def _load_deepseek_key_file() -> str:
    try:
        return _KEY_FILE.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, PermissionError, OSError):
        return ""


def load_deepseek_key() -> str:
    """读 keychain (macOS),为空再读文件兜底。"""
    if sys.platform == "darwin":
        try:
            key = subprocess.run(
                ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
                 "-a", KEYCHAIN_ACCOUNT, "-w"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            if key:
                return key
        except Exception:
            pass
    return _load_deepseek_key_file()


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

# v3.2: UI 显示用的视频类型标签 — key 不变, prompt 内部描述保留 (写得扎实没必要重写)
# 命名原则: 用用户视角的口语化表达, 一眼知道这个类型是干啥的
VIDEO_TYPE_LABELS: dict[str, str] = {
    "auto": "AI 智能",
    "product_recommendation": "卖货种草",
    "talking_explanation": "知识讲解",
    "atmosphere": "日常记录",
    "music_beat": "音乐卡点",
    "random_mix": "素材混剪",
}
# 视频类型 → 默认文案风格 (主控联动副控, B+C 设计)
VIDEO_TYPE_DEFAULT_STYLE: dict[str, str | None] = {
    "auto": None,                    # AI 智能不预设
    "product_recommendation": "种草推荐",   # 卖货种草 → 带货博主
    "talking_explanation": "知识科普",      # 知识讲解 → 科普老师
    "atmosphere": "励志感悟",                # 日常记录 → 走心姐姐
    "music_beat": "种草推荐",                # 音乐卡点 → 带货博主 (卡点视频也多是带货)
    "random_mix": None,                # 混剪不预设
}
# v3.2: UI 上加 "?" 图标的 tooltip 文案 (下拉菜单用户不容易发现提示, ? 图标更明显)
VIDEO_TYPE_TOOLTIP = "决定 AI 怎么组织文案的结构和节奏"


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
