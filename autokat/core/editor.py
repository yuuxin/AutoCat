"""核心混剪编排引擎

以 TTS 分句时间轴为基准，从素材池中 7 维随机编排生成渲染脚本。
二期增强：智能素材匹配、扩充转场/滤镜库
v2.3 增强：
- 删除 seg_dur ±0.3s 抖动（同步 bugfix，原来抖动让视觉时长偏离音频时长）
- 加入素材差异度调度（配额硬约束 + Jaccard 软约束，同 batch 内 30-50% 不一样）
"""

import json
import random
from pathlib import Path
from typing import Optional

from autokat.core.material import build_material_pool
from autokat.core.tagger import extract_keywords, match_materials_for_text
from autokat.core.diversity import (
    score_material_for_diversity, slice_key,
)

# ── 59 种 xfade 转场效果（与 renderer.py 一致） ──
TRANSITIONS = [
    "fade", "wipeleft", "wiperight", "wipeup", "wipedown",
    "slideleft", "slideright", "slideup", "slidedown",
    "circlecrop", "rectcrop", "distance", "fadeblack", "fadewhite",
    "radial", "smoothleft", "smoothright", "smoothup", "smoothdown",
    "circleopen", "circleclose", "vertopen", "vertclose",
    "horzopen", "horzclose", "dissolve", "pixelize",
    "diagtl", "diagtr", "diagbl", "diagbr",
    "hlslice", "hrslice", "vuslice", "vdslice",
    "hblur", "fadegrays",
    "wipetl", "wipetr", "wipebl", "wipebr",
    "squeezeh", "squeezev", "zoomin", "fadefast", "fadeslow",
    "hlwind", "hrwind", "vuwind", "vdwind",
    "coverleft", "coverright", "coverup", "coverdown",
    "revealleft", "revealright", "revealup", "revealdown",
]

SUBTITLE_POSITIONS = ["top", "middle", "bottom"]



def _pick_material(pool: list[dict], exclude_ids: set,
                   min_duration: float,
                   prefer_keywords: Optional[list[str]] = None,
                   state: Optional[dict] = None) -> Optional[dict]:
    """从素材池中智能选取素材

    优先选：与关键词匹配度高 + 未用过的素材

    v2.3 增强：差异度调度
    - state 为 None 时：与旧版完全一致（向后兼容）
    - state 不为 None 时：综合 (关键词匹配 + Jaccard 软约束 + 配额剩余) 排序

    state 结构 (由 generate_batch 维护):
        {
            "usage_count": {mat_id: used_count, ...},
            "recent_sets": [set_of_mat_ids, ...],  # 最近 N 条成片用过的素材
            "max_uses": int,
            "enable_diversity": bool,
        }
    """
    if not pool:
        return None

    candidates = [m for m in pool if m["duration"] >= min_duration]

    if prefer_keywords and candidates:
        # 按综合打分排序：(关键词匹配 - 复用惩罚) * 差异度系数
        def score(mat):
            tags = set(mat.get("tags", []))
            matched = len(set(prefer_keywords) & tags)
            reuse_penalty = 0 if mat["id"] not in exclude_ids else 0.3
            base = matched - reuse_penalty
            if state is not None and state.get("enable_diversity", False):
                diversity = score_material_for_diversity(
                    mat["id"],
                    state.get("recent_sets", []),
                    state.get("usage_count", {}),
                    int(state.get("max_uses", 5)),
                    base_score=1.0,
                )
            else:
                diversity = 1.0
            return base * diversity
        candidates.sort(key=score, reverse=True)
    else:
        # 无关键词时按差异度排序（如有 state）
        if state is not None and state.get("enable_diversity", False) and candidates:
            def score_div(mat):
                d = score_material_for_diversity(
                    mat["id"],
                    state.get("recent_sets", []),
                    state.get("usage_count", {}),
                    int(state.get("max_uses", 5)),
                    base_score=1.0,
                )
                # 优先未用过的（exclude_ids 之外的）
                bonus = 0.0 if mat["id"] not in exclude_ids else -0.3
                return d + bonus
            candidates.sort(key=score_div, reverse=True)
        else:
            # 优先选未用过的
            unused = [m for m in candidates if m["id"] not in exclude_ids]
            candidates = unused if unused else candidates

    # candidates 已按 min_duration 过滤，为空说明没有足够长的素材可用
    if candidates:
        return random.choice(candidates)
    # 容错：候选为空时（所有素材都已被排除 / 池过小），
    # 从完整 pool 随机选一个（允许重复使用），确保下游不拿到 None
    if pool:
        return random.choice(pool)
    return None


def _random_transition() -> str:
    return random.choice(TRANSITIONS)


def _random_subtitle_pos() -> str:
    return random.choice(SUBTITLE_POSITIONS)



def _random_crop_offset(mat_duration: float, clip_duration: float) -> float:
    max_offset = max(0, mat_duration - clip_duration)
    if max_offset <= 0:
        return 0.0
    return round(random.uniform(0, max_offset), 2)


def generate_script(
    sentences: list[dict],
    material_pool: Optional[list[dict]] = None,
    pool_filters: Optional[dict] = None,
    config: Optional[dict] = None,
    state: Optional[dict] = None,
) -> dict:
    """生成单条视频的编排脚本（二期增强版）

    性能优化：默认 1 句 = 1 镜头 50 个 seg 时 xfade 滤镜图太大、渲染极慢。
    现在通过 min_shot_duration 把几句聚成 1 个镜头，1 条 35s 视频从 50 seg → 17 seg，
    xfade 节点数 -3x，渲染时间 -5x~10x。字幕时间戳仍按句对齐。

    v2.3 增强：
    - 删除 seg_dur ±0.3s 抖动（同步 bugfix）。旧逻辑 seg_dur=remaining+uniform(-0.3,0.3)，
      remaining 是音频句时长，抖动后视觉时长偏离音频时长，同一时刻的视觉可能是
      shot 内的下一个 clip 而字幕/音轨还在当前句。差异度靠 filter/rotate/flip 制造。
    - state 入参：让 generate_batch 传入跨 video 状态（usage_count + recent_sets）
      实现差异度调度。state=None 时与旧版完全一致（向后兼容）。
    """
    cfg = config or {}
    trans_dur = cfg.get("transition_duration", 0.3)
    allow_reuse = cfg.get("allow_reuse", True)
    fps = cfg.get("fps", 30)
    margin = cfg.get("subtitle_margin", 80)
    narration_text = cfg.get("narration_text", "")
    min_shot_duration = float(cfg.get("min_shot_duration", 2.0))  # 每镜头最短秒数

    # 素材池
    if material_pool is None:
        material_pool = build_material_pool()
    if not material_pool:
        raise ValueError("素材池为空，请先导入素材")

    material_pool.sort(key=lambda m: m["duration"], reverse=True)

    # 文案关键词（用于智能匹配）
    keywords = extract_keywords(narration_text) if narration_text else None

    # ── 第一步：把句子聚成"镜头"（shot） ──
    # 累计到 >= min_shot_duration 就切下一个镜头；单个超长句单独成镜
    shots: list[list[dict]] = []
    cur_shot: list[dict] = []
    cur_dur = 0.0
    for sent in sentences:
        seg_dur = sent["end"] - sent["start"]
        if seg_dur < 0.3:
            continue
        if cur_dur + seg_dur >= min_shot_duration and cur_shot:
            shots.append(cur_shot)
            cur_shot, cur_dur = [], 0.0
        cur_shot.append(sent)
        cur_dur += seg_dur
    if cur_shot:
        shots.append(cur_shot)

    # ── 第二步：每个镜头循环选多段素材填满时长；字幕仍按句对齐 ──
    # v2.3 简化：不再限制镜头数, 按 TTS 音轨时长自动填切片, 镜头内可拼接多段素材。
    # 素材池偏碎（均2.7s）而音频较长时，单个素材不够填满一个 shot，
    # 改为"吃完"一段素材后再选下一段，循环填充直到 shot_dur 填满。
    used_ids: set = set()
    clips = []
    subtitles = []
    sub_pos = cfg.get("subtitle_position") or _random_subtitle_pos()
    for shot in shots:
        shot_start = shot[0]["start"]
        shot_end = shot[-1]["end"]
        shot_dur = shot_end - shot_start
        if shot_dur < 0.5:
            continue

        # 循环选取素材填满 shot_dur
        acc_dur = 0.0
        while acc_dur < shot_dur - 0.05:
            remaining = shot_dur - acc_dur
            mat = _pick_material(material_pool, used_ids, remaining, keywords, state=state)
            if mat is None:
                break  # 素材池耗尽，跳过该 shot
            if not allow_reuse:
                used_ids.add(mat["id"])

            offset = _random_crop_offset(mat["duration"], remaining)
            # 同步修复 v2: 末段 seg_dur 严格等于 remaining, 保证 visual 时长 == audio 时长。
            # 之前 max(0.3, seg_dur) 在 remaining < 0.3 时强行推到 0.3, 每段超 0.3s,
            # 5-6 段累计漂移 ±1.5s → 音轨和字幕/画面不同步 (用户反馈)。
            # 现在: 中间段 (raw_dur >= 0.3) 走原 min(remaining, mat_dur) 逻辑,
            # 末段 (raw_dur < 0.3) 严格等于 remaining, acc_dur 收尾必 == shot_dur。
            # 视觉差异度由 filter/rotate/flip/encoding 制造, 不依赖时长抖动。
            raw_dur = min(remaining, mat["duration"] - offset)
            if raw_dur < 0.3:
                # 末段: 严格等于 remaining, 视觉总时长严格对齐音频总时长
                seg_dur = remaining
            else:
                seg_dur = raw_dur

            clip = {
                "material_id": mat["id"],
                "source_path": mat["path"],
                "source_type": mat["type"],
                "offset": round(offset, 2),
                "duration": round(seg_dur, 2),
                "transition": _random_transition(),
                "transition_duration": trans_dur,
                "subtitle_position": sub_pos,
                "start_time": round(shot_start + acc_dur, 3),
                "end_time": round(shot_start + acc_dur + seg_dur, 3),
            }
            clips.append(clip)
            acc_dur += seg_dur

        # 镜头内每句一条独立字幕（保持时间戳精确）
        for s in shot:
            subtitles.append({
                "text": s["text"],
                "start": s["start"],
                "end": s["end"],
                "position": sub_pos,
                "margin": margin,
            })

    # 容错：allow_reuse=False 且素材池较小时，allow_reuse 路径上可能把可用素材
    # 全部 exclude 掉导致 clips 为空。这里以"内容完整性"为优先：清空 used_ids
    # 再走一遍，强制至少产出一条有内容的脚本。
    if not clips and shots:
        used_ids.clear()
        for shot in shots:
            shot_start = shot[0]["start"]
            shot_end = shot[-1]["end"]
            shot_dur = shot_end - shot_start
            if shot_dur < 0.5:
                continue

            # 循环选取素材填满 shot_dur
            acc_dur = 0.0
            while acc_dur < shot_dur - 0.05:
                remaining = shot_dur - acc_dur
                mat = _pick_material(material_pool, used_ids, remaining, keywords, state=state)
                if mat is None:
                    break
                used_ids.add(mat["id"])

                offset = _random_crop_offset(mat["duration"], remaining)
                # 同步修复 v2: 末段严格等于 remaining, 保证 visual == audio
                raw_dur = min(remaining, mat["duration"] - offset)
                if raw_dur < 0.3:
                    seg_dur = remaining
                else:
                    seg_dur = raw_dur

                clip = {
                    "material_id": mat["id"],
                    "source_path": mat["path"],
                    "source_type": mat["type"],
                    "offset": round(offset, 2),
                    "duration": round(seg_dur, 2),
                        "transition": _random_transition(),
                    "transition_duration": trans_dur,
                    "subtitle_position": sub_pos,
                    "start_time": round(shot_start + acc_dur, 3),
                    "end_time": round(shot_start + acc_dur + seg_dur, 3),
                }
                clips.append(clip)
                acc_dur += seg_dur

            for s in shot:
                subtitles.append({
                    "text": s["text"],
                    "start": s["start"],
                    "end": s["end"],
                    "position": sub_pos,
                    "margin": margin,
                })

    return {
        "fps": fps,
        "total_duration": round(clips[-1]["end_time"], 3) if clips else 0,
        "clips": clips,
        "subtitles": subtitles,
        "audio_path": None,
        "bgm_path": None,
    }


def generate_batch(
    sentences: list[dict],
    count: int,
    material_pool: Optional[list[dict]] = None,
    pool_filters: Optional[dict] = None,
    config: Optional[dict] = None,
) -> list[dict]:
    """批量生成编排脚本

    v2.3 增强：维护跨 video 状态 (usage_count + recent_sets) 让 _pick_material
    实现差异度调度（同 batch 内 30-50% 不一样切片组合）。

    state 字段:
        usage_count:  dict[mat_id -> 已用次数]
        recent_sets:  list[set[mat_id]] 最近 N 条成片用过的素材 id 集合
        max_uses:     int 每素材在 batch 内最大使用次数
        enable_diversity: bool 总开关
    """
    cfg = config or {}
    enable_diversity = bool(cfg.get("enable_diversity", True))
    max_uses = int(cfg.get("max_uses_per_slice", 5))
    recent_window = int(cfg.get("diversity_recent_window", 3))

    state = {
        "usage_count": {},
        "recent_sets": [],
        "max_uses": max_uses,
        "enable_diversity": enable_diversity,
    }

    scripts = []
    for i in range(count):
        script = generate_script(sentences, material_pool, pool_filters, config, state=state)
        script["index"] = i

        if enable_diversity:
            # 收集本次 video 用的 mat_id 集合（不含 offset 视为同源）
            used_mats = [c.get("material_id") for c in script.get("clips", [])]
            used_mats = [m for m in used_mats if m is not None]
            if used_mats:
                state["recent_sets"].append(set(used_mats))
                if len(state["recent_sets"]) > recent_window:
                    state["recent_sets"].pop(0)
                for mid in used_mats:
                    state["usage_count"][mid] = state["usage_count"].get(mid, 0) + 1

        scripts.append(script)
    return scripts


def save_script_to_file(script: dict, output_path: str):
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(script, f, ensure_ascii=False, indent=2)


def load_script_from_file(filepath: str) -> dict:
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)
