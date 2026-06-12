"""差异化扰动配置中心 - 3 档强度 + 风险阈值 JSON 外部可调

不强依赖其他模块，可独立 import。
"""

import json
import random
from pathlib import Path
from typing import Optional


# 强度档位
LEVELS = ["off", "low", "med", "high"]


# 默认风险阈值（risk 比例 → 等级），用于风险评级
# - 0..yellow:  低风险（不警告）
# - yellow..orange: 中风险（黄）
# - orange..red: 高风险（橙）
# - red..∞: 极高风险（红 + 二次确认）
DEFAULT_RISK_THRESHOLDS = {
    "yellow": 0.5,
    "orange": 1.0,
    "red": 2.0,
}


# 3 档强度对应的扰动维度开关
# off=全关, low=低（仅必要维度）, med=中（推荐默认）, high=高（全开）
LEVEL_CONFIG = {
    "off": {
        "subtitle_style": False,
        "scale_rotate": False, "flip": False, "nonstd_resolution": False,
        "quota_enforce": False, "jaccard_soft": False,
        "tts_diversity": False, "bgm_jitter": False, "subtitle_position": False,
    },
    "low": {
        "subtitle_style": True,
        "scale_rotate": False, "flip": False, "nonstd_resolution": False,
        "quota_enforce": True, "jaccard_soft": False,
        "tts_diversity": False, "bgm_jitter": False, "subtitle_position": False,
    },
    "med": {  # 默认
        "subtitle_style": True,
        "scale_rotate": True, "flip": True, "nonstd_resolution": True,
        "quota_enforce": True, "jaccard_soft": True,
        "tts_diversity": False, "bgm_jitter": False, "subtitle_position": False,
    },
    "high": {
        "subtitle_style": True,
        "scale_rotate": True, "flip": True, "nonstd_resolution": True,
        "quota_enforce": True, "jaccard_soft": True,
        "tts_diversity": True, "bgm_jitter": True, "subtitle_position": True,
    },
}


# 各维度参数区间（用于 build_perturbation）
DEFAULT_RANGES = {
    "scale": (0.94, 1.06),
    "rotate_deg": (-2.0, 2.0),
    "translate_px": (-30, 30),
    "hflip_prob": 0.3,  # 水平翻转概率（无字幕段）
    "nonstd_resolution_pool": [
        (1080, 1920), (1072, 1920), (1088, 1920),
        (1080, 1904), (1080, 1936),
    ],
}


def is_level_enabled(level: str) -> bool:
    """某档是否开启任何扰动维度。off 返回 False。"""
    cfg = LEVEL_CONFIG.get(level, LEVEL_CONFIG["med"])
    return any(cfg.values())


def build_perturbation(level: str = "med", rng: Optional[random.Random] = None) -> dict:
    """根据强度档位生成一组随机扰动参数。

    Args:
        level: 强度档位 "off" / "low" / "med" / "high"
        rng: 可选随机数生成器（用于可复现测试）

    Returns:
        扰动参数 dict（被关掉的维度不会有对应字段）
    """
    rng = rng or random.Random()
    cfg = LEVEL_CONFIG.get(level, LEVEL_CONFIG["med"])
    r = DEFAULT_RANGES
    p: dict = {"level": level}

    if cfg["scale_rotate"]:
        p["scale"] = rng.uniform(*r["scale"])
        p["rotate_deg"] = rng.uniform(*r["rotate_deg"])
        p["tx_px"] = rng.randint(*r["translate_px"])
        p["ty_px"] = rng.randint(*r["translate_px"])
        # v2.4: 不再随机 pad 背景色 (禁止调色), pad 统一用 black
    if cfg["nonstd_resolution"]:
        p["resolution"] = rng.choice(r["nonstd_resolution_pool"])
    if cfg["flip"]:
        p["hflip"] = rng.random() < r["hflip_prob"]
    if cfg["subtitle_position"]:
        # 9 档网格：top-left/center/right, middle-..., bottom-...
        p["subtitle_grid_pos"] = (rng.randint(0, 2), rng.randint(0, 2))

    return p


def load_risk_thresholds(config_path: Optional[str] = None) -> dict:
    """从 JSON 加载风险阈值，文件不存在则返回默认。

    Args:
        config_path: JSON 文件路径，None 用默认 ~/.config/autokat/risk_thresholds.json
    """
    if config_path is None:
        config_path = str(Path.home() / ".config" / "autokat" / "risk_thresholds.json")
    try:
        with open(config_path, "r") as f:
            data = json.load(f)
        # merge: 只覆盖存在的 key，且必须是 0-10 之间的数字
        merged = dict(DEFAULT_RISK_THRESHOLDS)
        for k in DEFAULT_RISK_THRESHOLDS:
            if k in data and isinstance(data[k], (int, float)) and 0 < data[k] < 10:
                merged[k] = float(data[k])
        return merged
    except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError):
        return dict(DEFAULT_RISK_THRESHOLDS)


def save_risk_thresholds(thresholds: dict, config_path: Optional[str] = None) -> bool:
    """保存风险阈值到 JSON。"""
    if config_path is None:
        config_path = str(Path.home() / ".config" / "autokat" / "risk_thresholds.json")
    try:
        Path(config_path).parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w") as f:
            json.dump(thresholds, f, indent=2)
        return True
    except (OSError, ValueError):
        return False
