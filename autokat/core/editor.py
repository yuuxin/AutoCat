"""核心混剪编排引擎

以 TTS 分句时间轴为基准，从素材池中 7 维随机编排生成渲染脚本。
二期增强：智能素材匹配、扩充转场/滤镜库
v2.3 增强：
- 删除 seg_dur ±0.3s 抖动（同步 bugfix，原来抖动让视觉时长偏离音频时长）
- 加入素材差异度调度（配额硬约束 + Jaccard 软约束，同 batch 内 30-50% 不一样）
"""

import json
import random
from typing import Optional

from autokat.core.material import build_material_pool
from autokat.core.tagger import extract_keywords
from autokat.core.diversity import (
    build_diversity_report, max_jaccard,
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



def _source_id(mat: dict) -> int:
    return int(mat.get("source_id") or mat["id"])


def _pick_material(pool: list[dict], exclude_ids: set,
                   requested_duration: float,
                   prefer_keywords: Optional[list[str]] = None,
                   state: Optional[dict] = None,
                   exclude_source_ids: Optional[set] = None) -> Optional[dict]:
    """Select a material using strict least-used tiers before soft scoring."""
    if not pool:
        return None

    state = state or {}
    min_segment_duration = float(state.get("min_segment_duration", 0.3))
    required = min(float(requested_duration), min_segment_duration)
    candidates = [m for m in pool if float(m.get("duration") or 0) >= required]
    if not candidates:
        return None

    # Hard tier 1: do not reuse a slice inside one video while alternatives exist.
    unused_in_video = [m for m in candidates if int(m["id"]) not in exclude_ids]
    if unused_in_video:
        candidates = unused_in_video

    if state.get("enable_diversity", False):
        usage = state.get("usage_count", {})
        source_usage = state.get("source_usage_count", {})
        max_uses = max(1, int(state.get("max_uses", 5)))

        # Keep the configured cap while possible; content completeness wins if all hit it.
        under_cap = [m for m in candidates if usage.get(int(m["id"]), 0) < max_uses]
        if under_cap:
            candidates = under_cap

        # Hard tier 2: an already-used slice cannot beat a never/less-used slice.
        min_usage = min(usage.get(int(m["id"]), 0) for m in candidates)
        candidates = [m for m in candidates if usage.get(int(m["id"]), 0) == min_usage]

    # Hard tier 3: among equally used slices, avoid a source already in this video.
    exclude_source_ids = exclude_source_ids or set()
    unused_sources = [m for m in candidates if _source_id(m) not in exclude_source_ids]
    if unused_sources:
        candidates = unused_sources

    if state.get("enable_diversity", False):
        source_usage = state.get("source_usage_count", {})
        # Hard tier 4: balance original source videos among equally used slices.
        min_source_usage = min(source_usage.get(_source_id(m), 0) for m in candidates)
        candidates = [
            m for m in candidates
            if source_usage.get(_source_id(m), 0) == min_source_usage
        ]

    keyword_set = set(prefer_keywords or [])
    recent_sets = state.get("recent_sets", [])
    recent_source_sets = state.get("recent_source_sets", [])

    def soft_score(mat: dict) -> float:
        mid = int(mat["id"])
        source_id = _source_id(mat)
        keyword_score = len(keyword_set & set(mat.get("tags", []))) * 2.0
        recent_penalty = sum(mid in recent for recent in recent_sets)
        source_penalty = sum(source_id in recent for recent in recent_source_sets)
        duration_fit = min(float(mat["duration"]), float(requested_duration))
        return keyword_score - recent_penalty - source_penalty * 0.5 + duration_fit * 0.01

    candidates.sort(key=soft_score, reverse=True)
    top_k = max(1, int(state.get("selection_top_k", 5)))
    return random.choice(candidates[:top_k])


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
    used_source_ids: set = set()
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
            mat = _pick_material(
                material_pool, used_ids, remaining, keywords,
                state=state, exclude_source_ids=used_source_ids,
            )
            if mat is None:
                break  # 素材池耗尽，跳过该 shot
            used_ids.add(int(mat["id"]))
            used_source_ids.add(_source_id(mat))

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
                "source_id": _source_id(mat),
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
        used_source_ids.clear()
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
                mat = _pick_material(
                    material_pool, used_ids, remaining, keywords,
                    state=state, exclude_source_ids=used_source_ids,
                )
                if mat is None:
                    break
                used_ids.add(int(mat["id"]))
                used_source_ids.add(_source_id(mat))

                offset = _random_crop_offset(mat["duration"], remaining)
                # 同步修复 v2: 末段严格等于 remaining, 保证 visual == audio
                raw_dur = min(remaining, mat["duration"] - offset)
                if raw_dur < 0.3:
                    seg_dur = remaining
                else:
                    seg_dur = raw_dur

                clip = {
                    "material_id": mat["id"],
                    "source_id": _source_id(mat),
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
    sentence_groups: Optional[list[list[dict]]] = None,
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
    if material_pool is None:
        material_pool = build_material_pool()
    if not material_pool:
        raise ValueError("素材池为空，请先导入素材")
    enable_diversity = bool(cfg.get("enable_diversity", True))
    max_uses = int(cfg.get("max_uses_per_slice", 5))
    recent_window = int(cfg.get("diversity_recent_window", 3))
    max_attempts = max(1, int(cfg.get("diversity_retry_attempts", 4)))
    jaccard_target = float(cfg.get("diversity_jaccard_target", 0.5))
    source_jaccard_target = float(cfg.get("diversity_source_jaccard_target", 0.6))

    state = {
        "usage_count": {},
        "source_usage_count": {},
        "recent_sets": [],
        "recent_source_sets": [],
        "max_uses": max_uses,
        "enable_diversity": enable_diversity,
        "min_segment_duration": float(cfg.get("min_segment_duration", 0.3)),
        "selection_top_k": int(cfg.get("diversity_selection_top_k", 5)),
    }

    scripts = []
    for i in range(count):
        current_sentences = (
            sentence_groups[i] if sentence_groups and i < len(sentence_groups)
            else sentences
        )
        attempts = []
        for _ in range(max_attempts if enable_diversity else 1):
            candidate = generate_script(
                current_sentences, material_pool, pool_filters, config, state=state
            )
            slice_set = {
                int(c["material_id"]) for c in candidate.get("clips", [])
                if c.get("material_id") is not None
            }
            source_set = {
                int(c.get("source_id") or c["material_id"])
                for c in candidate.get("clips", [])
                if c.get("material_id") is not None
            }
            slice_similarity = max_jaccard(slice_set, state["recent_sets"])
            source_similarity = max_jaccard(source_set, state["recent_source_sets"])
            attempts.append((max(slice_similarity, source_similarity), candidate, slice_set, source_set))
            if slice_similarity <= jaccard_target and source_similarity <= source_jaccard_target:
                break

        _, script, used_set, used_source_set = min(attempts, key=lambda item: item[0])
        script["index"] = i

        if enable_diversity:
            used_mats = [c.get("material_id") for c in script.get("clips", [])]
            used_mats = [m for m in used_mats if m is not None]
            if used_mats:
                state["recent_sets"].append(used_set)
                state["recent_source_sets"].append(used_source_set)
                if len(state["recent_sets"]) > recent_window:
                    state["recent_sets"].pop(0)
                    state["recent_source_sets"].pop(0)
                for mid in used_mats:
                    state["usage_count"][mid] = state["usage_count"].get(mid, 0) + 1
                for source_id in [
                    c.get("source_id") or c.get("material_id")
                    for c in script.get("clips", [])
                    if c.get("material_id") is not None
                ]:
                    state["source_usage_count"][source_id] = (
                        state["source_usage_count"].get(source_id, 0) + 1
                    )

        scripts.append(script)
    report = build_diversity_report(scripts, material_pool)
    for script in scripts:
        script["diversity_report"] = report
    return scripts


def save_script_to_file(script: dict, output_path: str):
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(script, f, ensure_ascii=False, indent=2)


def load_script_from_file(filepath: str) -> dict:
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)
