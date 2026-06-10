"""4 平台预设 - 选中平台后自动 fill 到 UI 控件默认值

不做水印合成（用户明确要求）。
所有字段独立可覆盖 - 平台切换时只 fill 用户未手动改过的字段。
"""

from typing import Optional


PLATFORM_IDS = ["douyin", "tiktok", "kuaishou", "xiaohongshu"]

PLATFORM_DISPLAY = {
    "douyin": "\u6296\u97f3",
    "tiktok": "TikTok",
    "kuaishou": "\u5feb\u624b",
    "xiaohongshu": "\u5c0f\u7ea2\u4e66",
}


# 4 平台 preset（无水印字段）
# 每个 key 对应 config 里的字段，UI 控件初始化时从 config 读
PLATFORM_PRESETS: dict = {
    "douyin": {
        "resolution": (1080, 1920),
        "subtitle_size": 68,  # v2.4: 手机端中等字号
        "subtitle_position": "bottom",
        "subtitle_font": "SourceHanSansSC-Heavy",
        "tts_voice": "zh-CN-XiaoxiaoNeural",
        "bgm_style": "upbeat",
    },
    "tiktok": {
        "resolution": (1080, 1920),
        "subtitle_size": 68,  # v2.4: 手机端中等字号
        "subtitle_position": "bottom",
        "subtitle_font": "SourceHanSansSC-Medium",
        "tts_voice": "zh-CN-XiaoxiaoNeural",
        "bgm_style": "upbeat_electronic",
    },
    "kuaishou": {
        "resolution": (1080, 1920),
        "subtitle_size": 68,  # v2.4: 手机端中等字号
        "subtitle_position": "bottom",
        "subtitle_font": "AlibabaPuHuiTi",
        "tts_voice": "zh-CN-YunjianNeural",
        "bgm_style": "folk",
    },
    "xiaohongshu": {
        "resolution": (1080, 1440),  # 3:4
        "subtitle_size": 68,  # v2.4: 手机端中等字号
        "subtitle_position": "bottom",
        "subtitle_font": "PingFang-Medium",
        "tts_voice": "zh-CN-XiaoyiNeural",
        "bgm_style": "fresh",
    },
}


# 调色偏好 -> 一组预设（用于 perturbation 阶段调色）
# BGM 风格标签（用于后续 BGM 库筛选扩展）
BGM_STYLES = ["upbeat", "upbeat_electronic", "folk", "fresh"]


def get_preset(platform_id: str) -> dict:
    """获取某平台 preset, 找不到时返回 douyin 的（不抛错）。"""
    return PLATFORM_PRESETS.get(platform_id, PLATFORM_PRESETS["douyin"])


def apply_preset_to_config(config: dict, platform_id: str,
                           user_overridden_keys: Optional[set] = None) -> dict:
    """把平台 preset 填到 config 字典。

    只会覆盖 user_overridden_keys 里**没有**的字段。已手动改过的字段保留用户值。

    Args:
        config: 现有 config dict（会被修改并返回）
        platform_id: 平台 id
        user_overridden_keys: 用户已手动改过的字段集合

    Returns:
        修改后的 config dict
    """
    preset = get_preset(platform_id)
    overridden = user_overridden_keys or set()
    for key, value in preset.items():
        if key not in overridden:
            config[key] = value
    return config


def get_resolution(platform_id: str) -> tuple:
    """获取某平台的分辨率 (width, height)。"""
    return get_preset(platform_id).get("resolution", (1080, 1920))
