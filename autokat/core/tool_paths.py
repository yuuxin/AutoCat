"""Resolve private third-party tools without consulting the user's PATH."""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path


class ToolNotFoundError(RuntimeError):
    """Raised when a required bundled tool is unavailable."""


def _is_packaged() -> bool:
    return bool(getattr(sys, "frozen", False))


def tools_dir() -> Path:
    """Return the only directory from which AutoCat may load private tools."""
    configured = os.environ.get("AUTOKAT_TOOLS_DIR")
    if configured:
        return Path(configured).expanduser().resolve()

    if _is_packaged():
        executable = Path(sys.executable).resolve()
        contents = executable.parent.parent
        return contents / "Resources" / "tools"

    project_root = Path(__file__).resolve().parents[2]
    return project_root / "external_tools" / platform.machine()


def tool_path(name: str, *, required: bool = True) -> Path:
    """Resolve an executable by name from AutoCat's private tools directory."""
    env_name = f"AUTOKAT_{name.upper().replace('-', '_')}"
    configured = os.environ.get(env_name)
    candidate = Path(configured).expanduser().resolve() if configured else tools_dir() / name

    if required:
        if not candidate.is_file():
            raise ToolNotFoundError(
                f"缺少内置工具 {name}: {candidate}。"
                "请重新安装完整的 AutoCat 内测包，不能依赖 Homebrew 或系统 PATH。"
            )
        if not os.access(candidate, os.X_OK):
            raise ToolNotFoundError(f"内置工具没有执行权限: {candidate}")
    return candidate


def tool_environment() -> dict[str, str]:
    """Environment additions for child processes using bundled tools."""
    directory = tools_dir()
    return {
        "AUTOKAT_TOOLS_DIR": str(directory),
        "AUTOKAT_FFMPEG": str(directory / "ffmpeg"),
        "AUTOKAT_FFPROBE": str(directory / "ffprobe"),
    }
