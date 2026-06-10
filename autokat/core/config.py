"""集中管理 config dict - 任务级配置

老的 config 散落在 CLI args / UI 控件 / batch_cfg dict 三处。
本模块提供:
- 一致的访问入口
- 平台 preset 选完后 flatten 进去
- 默认值兜底
"""

from typing import Optional

from autokat.core.presets import apply_preset_to_config, PLATFORM_IDS


# config 的默认值（除了平台 preset 之外的）
DEFAULTS = {
    "max_uses_per_slice": 5,  # UI 可改（1-10）
    "perturbation_level": "med",  # off/low/med/high
    "platform": None,  # 必须先选才能生成
    "enable_diversity": True,  # 差异化扰动总开关
    "diversity_recent_window": 3,
    "diversity_retry_attempts": 4,
    "diversity_jaccard_target": 0.5,
    "diversity_source_jaccard_target": 0.6,
    "diversity_selection_top_k": 5,
    "min_segment_duration": 0.3,
    "subtitle_size": 32,
    "subtitle_font": "SourceHanSansSC-Heavy",
    "subtitle_position": "bottom",
    "tts_voice": "zh-CN-XiaoxiaoNeural",
    "bgm_style": "upbeat",
    "resolution": (1080, 1920),
    "fps": 30,
    "bitrate": "8M",
    "transition_duration": 0.3,
    "shot_duration": 2.0,
    "allow_reuse": True,
    "subtitle_margin": 80,
    "dedup_threshold": 0.78,  # 成片去重相似度阈值
}


def build_config(platform_id: Optional[str] = None,
                 user_overridden: Optional[set] = None,
                 overrides: Optional[dict] = None) -> dict:
    """构造一个完整的 config dict。

    顺序: DEFAULTS -> 平台 preset (如有) -> overrides (如有)

    Args:
        platform_id: 平台 id，None 跳过 preset
        user_overridden: 用户已手动改过的字段集合（平台切换时不覆盖）
        overrides: 调用方临时覆盖的字段（如 UI 传过来的）

    Returns:
        config dict
    """
    cfg = dict(DEFAULTS)
    if platform_id and platform_id in PLATFORM_IDS:
        apply_preset_to_config(cfg, platform_id, user_overridden)
    if overrides:
        cfg.update(overrides)
    return cfg
