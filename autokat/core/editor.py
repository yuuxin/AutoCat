"""核心混剪编排引擎

以 TTS 分句时间轴为基准，从素材池中 7 维随机编排生成渲染脚本。
二期增强：智能素材匹配、扩充转场/滤镜库
v2.3 增强：
- 删除 seg_dur ±0.3s 抖动（同步 bugfix，原来抖动让视觉时长偏离音频时长）
- 加入素材差异度调度（配额硬约束 + Jaccard 软约束，同 batch 内 30-50% 不一样）
"""

import json
import random
import copy
from typing import Optional

from autokat.core.material import build_material_pool
from autokat.core.tagger import extract_keywords
from autokat.core.diversity import (
    build_diversity_report, max_jaccard,
)
from autokat.core.timeline import apply_integer_timeline

# ── 59 种 xfade 转场效果（与 renderer.py 一致） ──
TRANSITIONS = [
    "fade", "wipeleft", "wiperight", "wipeup", "wipedown",
    "slideleft", "slideright", "slideup", "slidedown",
    "circlecrop", "rectcrop", "distance",
    "radial", "smoothleft", "smoothright", "smoothup", "smoothdown",
    "circleopen", "circleclose", "vertopen", "vertclose",
    "horzopen", "horzclose", "dissolve", "pixelize",
    "diagtl", "diagtr", "diagbl", "diagbr",
    "hlslice", "hrslice", "vuslice", "vdslice",
    "hblur",
    "wipetl", "wipetr", "wipebl", "wipebr",
    "squeezeh", "squeezev", "zoomin", "fadefast", "fadeslow",
    "hlwind", "hrwind", "vuwind", "vdwind",
    "coverleft", "coverright", "coverup", "coverdown",
    "revealleft", "revealright", "revealup", "revealdown",
]

SUBTITLE_POSITIONS = ["top", "middle", "bottom"]
PLANNER_VERSION = "integer-rhythm-v1"
INTENT_VERSION = "intent-v1"

# -- Per video-type rhythm/transition profile table --
# Each profile describes the visual cadence and transition taste for a
# video type, so the planner can pick shot lengths and transitions
# appropriate for product / talking-head / atmosphere / music / mix:
#   shot_min / shot_max:        per-shot duration bounds (seconds)
#   shots_per_minute:          target shot density (count / minute)
#   transition_pool:           preferred xfade names
#   transition_pick_prob:      probability of sampling from the pool
#   min_subtitle_gap:          minimum gap between subtitles (seconds)
#   semantic_weight:           keyword/capability match weight
#   visual_weight:             visual variation weight
PROFILE_VERSION = "rhythm-profile-v1"
VIDEO_TYPE_PROFILES: dict = {
    "product_recommendation": {
        "shot_min": 2.0,
        "shot_max": 4.0,
        "shots_per_minute": 18.0,
        "transition_pool": [
            "fade", "dissolve", "fadefast", "fadeslow",
            "smoothleft", "smoothright", "smoothup", "smoothdown",
            "squeezeh", "squeezev", "zoomin",
        ],
        "transition_pick_prob": 0.75,
        "min_subtitle_gap": 0.6,
        "semantic_weight": 2.0,
        "visual_weight": 1.0,
    },
    "talking_explanation": {
        "shot_min": 3.0,
        "shot_max": 6.0,
        "shots_per_minute": 12.0,
        "transition_pool": [
            "fade", "dissolve", "fadefast", "fadeslow",
            "smoothleft", "smoothright", "smoothup", "smoothdown",
        ],
        "transition_pick_prob": 0.85,
        "min_subtitle_gap": 1.0,
        "semantic_weight": 1.0,
        "visual_weight": 0.6,
    },
    "atmosphere": {
        "shot_min": 4.0,
        "shot_max": 7.0,
        "shots_per_minute": 10.0,
        "transition_pool": [
            "fade", "dissolve", "fadeslow",
            "smoothleft", "smoothright", "smoothup", "smoothdown",
            "circleopen", "circleclose", "vertopen", "vertclose",
            "horzopen", "horzclose",
        ],
        "transition_pick_prob": 0.9,
        "min_subtitle_gap": 1.2,
        "semantic_weight": 0.8,
        "visual_weight": 0.7,
    },
    "music_beat": {
        "shot_min": 1.5,
        "shot_max": 3.0,
        "shots_per_minute": 28.0,
        "transition_pool": [
            "wipeleft", "wiperight", "wipeup", "wipedown",
            "wipetl", "wipetr", "wipebl", "wipebr",
            "slideleft", "slideright", "slideup", "slidedown",
            "diagtl", "diagtr", "diagbl", "diagbr",
            "coverleft", "coverright", "coverup", "coverdown",
            "revealleft", "revealright", "revealup", "revealdown",
            "zoomin", "hlslice", "hrslice", "vuslice", "vdslice",
            "hlwind", "hrwind", "vuwind", "vdwind",
        ],
        "transition_pick_prob": 0.7,
        "min_subtitle_gap": 0.4,
        "semantic_weight": 0.5,
        "visual_weight": 1.4,
    },
    "random_mix": {
        "shot_min": 2.0,
        "shot_max": 5.0,
        "shots_per_minute": 16.0,
        "transition_pool": [
            "fade", "dissolve", "fadefast", "fadeslow",
            "wipeleft", "wiperight", "slideleft", "slideright",
            "smoothleft", "smoothright", "zoomin",
        ],
        "transition_pick_prob": 0.5,
        "min_subtitle_gap": 0.8,
        "semantic_weight": 1.0,
        "visual_weight": 1.0,
    },
}
# "auto" and unknown types fall back to random_mix for backward compat.
VIDEO_TYPE_PROFILES["auto"] = VIDEO_TYPE_PROFILES["random_mix"]


def video_type_profile(video_type: str) -> dict:
    """Return the rhythm/transition profile for a video type, defaulting to random_mix.

    Unrecognized or empty values fall back to random_mix so downstream
    callers can always index into the result safely.
    """
    key = str(video_type or "").strip().lower()
    return VIDEO_TYPE_PROFILES.get(key) or VIDEO_TYPE_PROFILES["random_mix"]




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
        semantic_score, _ = _semantic_match(mat, list(keyword_set))
        keyword_score = semantic_score * 2.0
        recent_penalty = sum(mid in recent for recent in recent_sets)
        source_penalty = sum(source_id in recent for recent in recent_source_sets)
        duration_fit = min(float(mat["duration"]), float(requested_duration))
        quality_score = float(mat.get("quality_score") or 0)
        return (
            keyword_score - recent_penalty - source_penalty * 0.5
            + duration_fit * 0.01 + quality_score * 0.25
        )

    candidates.sort(key=soft_score, reverse=True)
    top_k = max(1, int(state.get("selection_top_k", 5)))
    return random.choice(candidates[:top_k])


def _semantic_match(mat: dict, keywords: Optional[list[str]]) -> tuple[float, str]:
    """Score persisted capabilities against one shot's local subtitle intent."""
    wanted = {str(value).strip() for value in (keywords or []) if str(value).strip()}
    tags = {str(value).strip() for value in mat.get("tags", []) if str(value).strip()}
    direct = wanted & tags
    if direct:
        return float(len(direct)), "local_intent"
    summary = str(mat.get("capability_summary") or "")
    related = {word for word in wanted if word and word in summary}
    if related:
        return float(len(related)) * 0.5, "related_subject"
    return 0.0, "quality_fallback"


def _local_intent_keywords(text: str, global_keywords: Optional[list[str]] = None) -> list[str]:
    """Map one subtitle group to the small persisted capability vocabulary."""
    values = list(extract_keywords(text) or [])
    for label, needles in (
        ("女鞋", ("女鞋", "鞋子", "穿鞋", "双脚")),
        ("商品", ("商品", "产品", "推荐", "入手", "好物")),
        ("细节", ("细节", "局部", "设计", "做工")),
        ("人物", ("穿搭", "上身", "模特", "姐妹")),
        ("行走", ("走路", "步伐", "逛街", "通勤")),
        ("展示", ("展示", "看看", "亮点")),
    ):
        if any(needle in text for needle in needles):
            values.append(label)
    values.extend(global_keywords or [])
    return list(dict.fromkeys(values))


def _random_transition(profile: Optional[dict] = None) -> str:
    """Pick a transition, weighted by the per-video-type profile when given.

    Falls back to a uniform sample across the full TRANSITIONS list when no
    profile is supplied so existing callers keep the same distribution.
    """
    if profile:
        pool = profile.get("transition_pool") or TRANSITIONS
        prob = float(profile.get("transition_pick_prob", 0.5) or 0.0)
        if pool and random.random() < prob:
            return random.choice(pool)
    return random.choice(TRANSITIONS)


def _transition_overhead(clip_index: int, transition_duration: float) -> float:
    """Renderer applies xfade inside fixed groups of five clips."""
    return transition_duration if clip_index % 5 != 0 else 0.0


def _random_subtitle_pos() -> str:
    return random.choice(SUBTITLE_POSITIONS)



def _random_crop_offset(mat_duration: float, clip_duration: float) -> float:
    max_offset = max(0, mat_duration - clip_duration)
    if max_offset <= 0:
        return 0.0
    return round(random.uniform(0, max_offset), 2)


def _plan_shots_by_frames(visual_sentences: list[dict], fps: int,
                          narration_duration: float,
                          video_type: str = "auto") -> list[list[dict]]:
    """Plan semantic shot groups in integer-frame space without touching captions."""
    if not visual_sentences:
        return []
    total_frames = max(1, int(round(narration_duration * fps)))
    boundaries = [0]
    for sentence in visual_sentences:
        boundary = max(0, min(total_frames, int(round(float(sentence["end"]) * fps))))
        if boundary > boundaries[-1]:
            boundaries.append(boundary)
    if boundaries[-1] != total_frames:
        boundaries.append(total_frames)

    # Pull shot duration bounds from the per-type rhythm profile, with
    # a long-form safeguard so videos over 30s keep room for stable shots.
    _profile = video_type_profile(video_type)
    _long_form = narration_duration >= 30.0
    _default_min = 3.0 if _long_form else 2.0
    _default_max = 6.0 if _long_form else 4.0
    preferred = (
        float(_profile.get("shot_min", _default_min)),
        float(_profile.get("shot_max", _default_max)),
    )
    normal_min_frames = int(round(1.5 * fps))
    tail_fragment_frames = int(round(1.0 * fps))
    hook_end_frames = int(round(3.0 * fps))
    hook_min_frames = int(round(1.0 * fps))
    hook_max_frames = int(round(2.5 * fps))
    preferred_min = int(round(preferred[0] * fps))
    preferred_max = int(round(preferred[1] * fps))

    # DP over caption boundaries. Cost strongly rejects abnormal short shots and
    # sub-second trailing fragments, while allowing stable shots across captions.
    count = len(boundaries)
    best = [float("inf")] * count
    prev = [-1] * count
    best[0] = 0.0
    for end_idx in range(1, count):
        for start_idx in range(end_idx):
            if best[start_idx] == float("inf"):
                continue
            start_frame, end_frame = boundaries[start_idx], boundaries[end_idx]
            duration = end_frame - start_frame
            is_hook = start_frame < hook_end_frames
            min_frames = hook_min_frames if is_hook else normal_min_frames
            max_frames = hook_max_frames if is_hook else preferred_max
            cost = 0.0
            if duration < min_frames:
                cost += 1000.0 + (min_frames - duration) * 10.0
            if end_idx == count - 1 and duration < tail_fragment_frames:
                cost += 3000.0
            if duration < preferred_min:
                cost += (preferred_min - duration) / max(1, fps)
            elif duration > max_frames:
                cost += (duration - max_frames) / max(1, fps) * 0.35
            target = (preferred_min + max_frames) / 2
            cost += abs(duration - target) / max(1, fps) * 0.05
            candidate = best[start_idx] + cost
            if candidate < best[end_idx]:
                best[end_idx], prev[end_idx] = candidate, start_idx

    ranges = []
    cursor = count - 1
    while cursor > 0 and prev[cursor] >= 0:
        ranges.append((boundaries[prev[cursor]], boundaries[cursor]))
        cursor = prev[cursor]
    ranges.reverse()
    if not ranges:
        ranges = [(0, total_frames)]

    shots = []
    for start_frame, end_frame in ranges:
        shot = [
            sentence for sentence in visual_sentences
            if int(round(float(sentence["start"]) * fps)) < end_frame
            and int(round(float(sentence["end"]) * fps)) > start_frame
        ]
        if shot:
            shots.append(shot)
    return shots


def _script_signatures(script: dict, fps: int) -> dict:
    clips = [clip for clip in script.get("clips", []) if not clip.get("is_tail")]
    hook_limit = int(round(3.0 * fps))
    hook = tuple(
        int(clip.get("source_id") or clip.get("material_id"))
        for clip in clips if int(clip.get("start_frame", 0)) < hook_limit
    )
    order = tuple(int(clip.get("source_id") or clip.get("material_id")) for clip in clips)
    ending = tuple(order[-2:])
    return {"hook": hook, "order": order, "ending": ending}


def detect_video_type(narration_text: str) -> str:
    text = narration_text or ""
    if any(word in text for word in ("推荐", "入手", "搭配", "好物", "商品")):
        return "product_recommendation"
    if any(word in text for word in ("讲解", "教程", "步骤", "知识", "为什么")):
        return "talking_explanation"
    if any(word in text for word in ("氛围", "记录", "日常", "旅行", "治愈")):
        return "atmosphere"
    if any(word in text for word in ("卡点", "节奏", "音乐")):
        return "music_beat"
    return "random_mix"


def _signature_similarity(signatures: dict, previous: list[dict]) -> float:
    if not previous:
        return 0.0
    current_order = signatures["order"]
    scores = []
    for item in previous:
        hook_same = float(signatures["hook"] == item["hook"] and bool(signatures["hook"]))
        end_same = float(signatures["ending"] == item["ending"] and bool(signatures["ending"]))
        limit = min(len(current_order), len(item["order"]))
        order_same = (
            sum(a == b for a, b in zip(current_order[:limit], item["order"][:limit]))
            / max(1, limit)
        )
        scores.append(max(hook_same, end_same, order_same))
    return max(scores)


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
    original_sentences = copy.deepcopy(sentences)
    trans_dur = cfg.get("transition_duration", 0.3)
    fps = cfg.get("fps", 30)
    margin = cfg.get("subtitle_margin", 80)
    narration_text = cfg.get("narration_text", "")
    source_safety_margin = float(cfg.get("source_safety_margin", 0.35))
    video_type = str(cfg.get("video_type", "auto"))
    if video_type == "auto":
        video_type = detect_video_type(narration_text)
    profile = video_type_profile(video_type)

    # 素材池
    if material_pool is None:
        material_pool = build_material_pool()
    if not material_pool:
        raise ValueError("素材池为空，请先导入素材")

    material_pool.sort(key=lambda m: m["duration"], reverse=True)

    # 文案关键词（用于智能匹配）
    keywords = extract_keywords(narration_text) if narration_text else None

    # Subtitles keep exact TTS word-boundary times. Visual intervals fill pauses
    # between captions and the trailing audio silence so the picture never ends early.
    subtitle_sentences = [dict(sentence) for sentence in sentences]
    narration_duration = float(
        (sentences[0].get("_narration_duration") if sentences else 0)
        or (sentences[-1]["end"] if sentences else 0)
    )
    visual_sentences = []
    for index, sentence in enumerate(subtitle_sentences):
        visual = dict(sentence)
        visual["_subtitle_start"] = sentence["start"]
        visual["_subtitle_end"] = sentence["end"]
        visual["start"] = 0.0 if index == 0 else float(sentence["start"])
        visual["end"] = (
            float(subtitle_sentences[index + 1]["start"])
            if index + 1 < len(subtitle_sentences)
            else narration_duration
        )
        visual_sentences.append(visual)

    # ── 第一步：整数帧动态镜头规划 ──
    shots = _plan_shots_by_frames(visual_sentences, fps, narration_duration, video_type)

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
        shot_keywords = _local_intent_keywords(
            "".join(str(item.get("text", "")) for item in shot), keywords,
        )

        # 循环选取素材填满 shot_dur
        acc_dur = 0.0
        while acc_dur < shot_dur - 0.05:
            remaining = shot_dur - acc_dur
            overhead = _transition_overhead(len(clips), trans_dur)
            mat = _pick_material(
                material_pool, used_ids, remaining + overhead, shot_keywords,
                state=state, exclude_source_ids=used_source_ids,
            )
            if mat is None:
                break  # 素材池耗尽，跳过该 shot
            used_ids.add(int(mat["id"]))
            used_source_ids.add(_source_id(mat))

            usable_duration = max(0.0, mat["duration"] - source_safety_margin)
            content_dur = min(remaining, max(0.0, usable_duration - overhead))
            if content_dur < 0.05:
                break
            render_dur = content_dur + overhead
            local_offset = _random_crop_offset(usable_duration, render_dur)
            offset = local_offset + float(mat.get("base_offset", 0))
            # 同步修复 v2: 末段 seg_dur 严格等于 remaining, 保证 visual 时长 == audio 时长。
            # 之前 max(0.3, seg_dur) 在 remaining < 0.3 时强行推到 0.3, 每段超 0.3s,
            # 5-6 段累计漂移 ±1.5s → 音轨和字幕/画面不同步 (用户反馈)。
            # 现在: 中间段 (raw_dur >= 0.3) 走原 min(remaining, mat_dur) 逻辑,
            # 末段 (raw_dur < 0.3) 严格等于 remaining, acc_dur 收尾必 == shot_dur。
            # 视觉差异度由 filter/rotate/flip/encoding 制造, 不依赖时长抖动。
            raw_dur = min(remaining, usable_duration - local_offset - overhead)
            seg_dur = raw_dur

            clip = {
                "material_id": mat["id"],
                "virtual_slice_id": mat.get("virtual_slice_id"),
                "source_id": _source_id(mat),
                "source_path": mat["path"],
                "source_type": mat["type"],
                "offset": round(offset, 2),
                "duration": round(seg_dur + overhead, 2),
                "transition": _random_transition(profile),
                "transition_duration": trans_dur,
                "subtitle_position": sub_pos,
                "start_time": round(shot_start + acc_dur, 3),
                "end_time": round(shot_start + acc_dur + seg_dur, 3),
            }
            clip["semantic_score"], clip["semantic_match_level"] = _semantic_match(
                mat, shot_keywords,
            )
            clips.append(clip)
            acc_dur += seg_dur

        # 镜头内每句一条独立字幕（保持时间戳精确）
        for s in shot:
            subtitles.append({
                "text": s["text"],
                "start": s.get("_subtitle_start", s["start"]),
                "end": s.get("_subtitle_end", s["end"]),
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
            shot_keywords = _local_intent_keywords(
                "".join(str(item.get("text", "")) for item in shot), keywords,
            )

            # 循环选取素材填满 shot_dur
            acc_dur = 0.0
            while acc_dur < shot_dur - 0.05:
                remaining = shot_dur - acc_dur
                overhead = _transition_overhead(len(clips), trans_dur)
                mat = _pick_material(
                    material_pool, used_ids, remaining + overhead, shot_keywords,
                    state=state, exclude_source_ids=used_source_ids,
                )
                if mat is None:
                    break
                used_ids.add(int(mat["id"]))
                used_source_ids.add(_source_id(mat))

                usable_duration = max(0.0, mat["duration"] - source_safety_margin)
                content_dur = min(remaining, max(0.0, usable_duration - overhead))
                if content_dur < 0.05:
                    break
                render_dur = content_dur + overhead
                local_offset = _random_crop_offset(usable_duration, render_dur)
                offset = local_offset + float(mat.get("base_offset", 0))
                # 同步修复 v2: 末段严格等于 remaining, 保证 visual == audio
                raw_dur = min(remaining, usable_duration - local_offset - overhead)
                seg_dur = raw_dur

                clip = {
                    "material_id": mat["id"],
                    "virtual_slice_id": mat.get("virtual_slice_id"),
                    "source_id": _source_id(mat),
                    "source_path": mat["path"],
                    "source_type": mat["type"],
                    "offset": round(offset, 2),
                    "duration": round(seg_dur + overhead, 2),
                        "transition": _random_transition(profile),
                    "transition_duration": trans_dur,
                    "subtitle_position": sub_pos,
                    "start_time": round(shot_start + acc_dur, 3),
                    "end_time": round(shot_start + acc_dur + seg_dur, 3),
                }
                clip["semantic_score"], clip["semantic_match_level"] = _semantic_match(
                    mat, shot_keywords,
                )
                clips.append(clip)
                acc_dur += seg_dur

            for s in shot:
                subtitles.append({
                    "text": s["text"],
                    "start": s.get("_subtitle_start", s["start"]),
                    "end": s.get("_subtitle_end", s["end"]),
                    "position": sub_pos,
                    "margin": margin,
                })

    tail_duration = float(cfg.get("tail_duration", 0.5))
    if clips and tail_duration > 0:
        overhead = _transition_overhead(len(clips), trans_dur)
        tail_pool = [
            material for material in material_pool
            if float(material.get("duration") or 0) - source_safety_margin
            >= tail_duration + overhead
        ]
        used_tail_pool = [
            material for material in tail_pool if int(material["id"]) in used_ids
        ]
        if used_tail_pool:
            tail_pool = used_tail_pool
        tail_mat = _pick_material(
            tail_pool, set(), tail_duration + overhead, keywords,
            state=state, exclude_source_ids=used_source_ids,
        )
        if tail_mat is not None and tail_mat["duration"] >= tail_duration + overhead:
            offset = _random_crop_offset(
                tail_mat["duration"] - source_safety_margin,
                tail_duration + overhead,
            )
            offset += float(tail_mat.get("base_offset", 0))
            clips.append({
                "material_id": tail_mat["id"],
                "virtual_slice_id": tail_mat.get("virtual_slice_id"),
                "source_id": _source_id(tail_mat),
                "source_path": tail_mat["path"],
                "source_type": tail_mat["type"],
                "offset": round(offset, 2),
                "duration": round(tail_duration + overhead, 2),
                "transition": _random_transition(profile),
                "transition_duration": trans_dur,
                "subtitle_position": sub_pos,
                "start_time": round(narration_duration, 3),
                "end_time": round(narration_duration + tail_duration, 3),
                "is_tail": True,
            })

    script = {
        "schema_version": 2,
        "planner_version": PLANNER_VERSION,
        "intent_version": INTENT_VERSION,
        "profile_version": PROFILE_VERSION,
        "active_profile": dict(profile),
        "video_type": video_type,
        "fps": fps,
        "transition_duration": trans_dur,
        "clips": clips,
        "subtitles": subtitles,
        "audio_path": None,
        "bgm_path": None,
        "planned_shots": [
            {
                "start_frame": int(round(float(shot[0]["start"]) * fps)),
                "end_frame": int(round(float(shot[-1]["end"]) * fps)),
                "subtitle_count": len(shot),
            }
            for shot in shots
        ],
    }
    result = apply_integer_timeline(
        script, narration_duration=narration_duration, fps=fps,
        tail_duration=tail_duration,
    )
    if sentences != original_sentences:
        raise RuntimeError("镜头编排修改了受保护的字幕时间轴")
    return result


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
    max_attempts = max(1, int(cfg.get("diversity_retry_attempts", 6)))
    jaccard_target = float(cfg.get("diversity_jaccard_target", 0.5))
    source_jaccard_target = float(cfg.get("diversity_source_jaccard_target", 0.6))

    state = {
        "usage_count": {},
        "source_usage_count": {},
        "recent_sets": [],
        "recent_source_sets": [],
        "recent_signatures": [],
        "used_hook_signatures": set(),
        "used_ending_signatures": set(),
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
                if c.get("material_id") is not None and not c.get("is_tail")
            }
            source_set = {
                int(c.get("source_id") or c["material_id"])
                for c in candidate.get("clips", [])
                if c.get("material_id") is not None and not c.get("is_tail")
            }
            slice_similarity = max_jaccard(slice_set, state["recent_sets"])
            source_similarity = max_jaccard(source_set, state["recent_source_sets"])
            signatures = _script_signatures(candidate, int(cfg.get("fps", 30)))
            signature_similarity = _signature_similarity(signatures, state["recent_signatures"])
            global_signature_penalty = (
                0.6 * float(signatures["hook"] in state["used_hook_signatures"])
                + 0.4 * float(signatures["ending"] in state["used_ending_signatures"])
            )
            attempts.append((
                max(slice_similarity, source_similarity, signature_similarity)
                + global_signature_penalty,
                candidate, slice_set, source_set, signatures,
            ))
            if (
                slice_similarity <= jaccard_target
                and source_similarity <= source_jaccard_target
                and signature_similarity < 1.0
                and global_signature_penalty == 0
            ):
                break

        _, script, used_set, used_source_set, signatures = min(attempts, key=lambda item: item[0])
        script["index"] = i

        if enable_diversity:
            used_mats = [
                c.get("material_id") for c in script.get("clips", [])
                if not c.get("is_tail")
            ]
            used_mats = [m for m in used_mats if m is not None]
            if used_mats:
                state["recent_sets"].append(used_set)
                state["recent_source_sets"].append(used_source_set)
                state["recent_signatures"].append(signatures)
                state["used_hook_signatures"].add(signatures["hook"])
                state["used_ending_signatures"].add(signatures["ending"])
                if len(state["recent_sets"]) > recent_window:
                    state["recent_sets"].pop(0)
                    state["recent_source_sets"].pop(0)
                    state["recent_signatures"].pop(0)
                for mid in used_mats:
                    state["usage_count"][mid] = state["usage_count"].get(mid, 0) + 1
                for source_id in [
                    c.get("source_id") or c.get("material_id")
                    for c in script.get("clips", [])
                    if c.get("material_id") is not None and not c.get("is_tail")
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
