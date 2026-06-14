"""Wizard UI snapshot schema, field labels, and helpers.

The snapshot is a JSON blob captured at task creation time. It lets any
existing task be reopened in the wizard in either of two modes:

* ``view``  - read-only audit, every widget is disabled, the step bar is
  clickable so the user can flip between Steps 1-3 to inspect the design.
* ``fork``  - editable copy, every widget is prefilled with the snapshot
  values, the user can change anything and start a new task.

Old tasks created before the M4 migration (column ``tasks.wizard_snapshot``)
have NULL here and gracefully degrade to the legacy ``tasks.config`` subset.
"""
from __future__ import annotations

from typing import Any

# Single source of truth for "what the user sees" labels. Both the snapshot
# capture code and the legacy-config fallback read from this dict so a
# future change to the wizard UI only needs one update.
WIZARD_FIELD_LABELS: dict[str, str] = {
    # Step 1
    "selected_material_ids": "选中的素材数",
    "tag_filter": "标签筛选",
    # Step 2
    "script_text": "口播文案",
    "script_name": "文案名称",
    "lang": "文案语言",
    "voice": "TTS 音色",
    "rate": "语速",
    "pitch": "音调",
    "writer_provider": "AI 文案模型",
    # Step 3
    "task_name": "任务名称",
    "count": "生成数量",
    "workers": "并发进程",
    "fps": "帧率",
    "enable_bgm": "是否启用 BGM",
    "bgm_volume": "BGM 音量",
    "max_uses_per_slice": "切片最大复用次数",
    "enable_diversity": "启用差异化",
    "perturbation_level": "差异化扰动档位",
    "dedup_threshold": "去重阈值",
    "subtitle_font": "字幕字体",
    "font_size": "字幕字号",
    "platform": "发布平台",
    "video_type": "视频类型",
}


def empty_snapshot() -> dict[str, Any]:
    """Return a snapshot with every field set to None.

    Useful as a starting point for ``_capture_wizard_snapshot`` and as a
    safe default when restoring a task that lacks any stored snapshot.
    """
    return {"schema_version": 1, "fields": {key: None for key in WIZARD_FIELD_LABELS}}


def label_for(field_key: str) -> str:
    """Return the Chinese display label for a snapshot field key."""
    return WIZARD_FIELD_LABELS.get(field_key, field_key)
