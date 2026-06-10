"""切片组合差异度调度 + 风险评级

每条成片 = 12-15 个素材切片。同 batch 内要让两两条之间至少 30-50% 不一样。
两套机制:
- 硬约束：每个切片在 batch 内最多用 K 次（默认 5，UI 可改 1-10）
- 软约束：与最近 N 条的 Jaccard 相似度 <= 0.5

不算 state, 纯函数式; 调用方在 generate_batch 循环里维护 recent_sets 和 usage_count。
"""

from typing import Optional


# 默认参数（与 perturbation.py 的 quota 保持一致）
DEFAULT_MAX_USES_PER_SLICE = 5
DEFAULT_RECENT_WINDOW = 3
DEFAULT_JACCARD_TARGET = 0.5  # 目标 Jaccard <= 0.5 (即 >= 50% 不一样)


def slice_key(material_id, offset) -> tuple:
    """生成切片的唯一标识。

    即使同一素材的不同 offset 切片也算不同切片（这与 editor.py 里
    material_id + offset 作为 clip identity 的语义一致）。
    """
    return (int(material_id), round(float(offset), 2))


def compute_jaccard(set_a: set, set_b: set) -> float:
    """Jaccard 相似度: |A & B| / |A | B|, 0=完全不同, 1=完全相同。"""
    if not set_a and not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union > 0 else 0.0


def max_jaccard(target: set, previous_sets: list[set]) -> float:
    """Return the highest Jaccard similarity against previous combinations."""
    if not previous_sets:
        return 0.0
    return max(compute_jaccard(target, previous) for previous in previous_sets)


def build_diversity_report(scripts: list[dict], material_pool: list[dict]) -> dict:
    """Summarize slice/source coverage and pairwise combination similarity."""
    available_ids = {int(m["id"]) for m in material_pool}
    available_sources = {
        int(m.get("source_id") or m["id"]) for m in material_pool
    }
    usage: dict[int, int] = {}
    source_usage: dict[int, int] = {}
    slice_sets = []
    source_sets = []

    for script in scripts:
        script_ids = []
        script_sources = []
        for clip in script.get("clips", []):
            mid = clip.get("material_id")
            if mid is None:
                continue
            mid = int(mid)
            source_id = int(clip.get("source_id") or mid)
            usage[mid] = usage.get(mid, 0) + 1
            source_usage[source_id] = source_usage.get(source_id, 0) + 1
            script_ids.append(mid)
            script_sources.append(source_id)
        slice_sets.append(set(script_ids))
        source_sets.append(set(script_sources))

    pair_slice_jaccards = []
    pair_source_jaccards = []
    for i in range(len(slice_sets)):
        for j in range(i + 1, len(slice_sets)):
            pair_slice_jaccards.append(compute_jaccard(slice_sets[i], slice_sets[j]))
            pair_source_jaccards.append(compute_jaccard(source_sets[i], source_sets[j]))

    return {
        "slice_coverage": len(usage) / len(available_ids) if available_ids else 0.0,
        "source_coverage": len(source_usage) / len(available_sources) if available_sources else 0.0,
        "unused_slices": max(0, len(available_ids) - len(usage)),
        "max_slice_uses": max(usage.values(), default=0),
        "max_source_uses": max(source_usage.values(), default=0),
        "avg_slice_uses": sum(usage.values()) / len(usage) if usage else 0.0,
        "max_slice_jaccard": max(pair_slice_jaccards, default=0.0),
        "avg_slice_jaccard": (
            sum(pair_slice_jaccards) / len(pair_slice_jaccards)
            if pair_slice_jaccards else 0.0
        ),
        "max_source_jaccard": max(pair_source_jaccards, default=0.0),
    }


def compute_risk_level(desired_count: int, max_safe_count: int,
                       thresholds: Optional[dict] = None) -> str:
    """算风险等级: low / medium / high / extreme

    - desired <= yellow * max_safe: low
    - yellow < desired <= orange * max_safe: medium
    - orange < desired <= red * max_safe: high
    - desired > red * max_safe: extreme

    thresholds 默认从 perturbation.load_risk_thresholds 读。
    """
    if max_safe_count <= 0 or desired_count <= 0:
        return "low"
    if thresholds is None:
        from autokat.core.perturbation import load_risk_thresholds
        thresholds = load_risk_thresholds()
    y = thresholds.get("yellow", 0.5)
    o = thresholds.get("orange", 1.0)
    r = thresholds.get("red", 2.0)
    ratio = desired_count / max_safe_count
    if ratio <= y:
        return "low"
    elif ratio <= o:
        return "medium"
    elif ratio <= r:
        return "high"
    else:
        return "extreme"


def score_material_for_diversity(
    material_id,
    recent_sets: list,
    usage_count: dict,
    max_uses: int,
    jaccard_target: float = DEFAULT_JACCARD_TARGET,
    base_score: float = 1.0,
) -> float:
    """算素材的差异化打分（越高越优先选）。

    惩罚项:
    - 该素材已被用满 max_uses 次: 返回 0（不可用）
    - 该素材在最近 N 条的 Jaccard 平均 > jaccard_target: 降权

    Args:
        material_id: 素材 id
        recent_sets: 最近 N 条成片的切片集合（list[set[slice_key]]）
        usage_count: dict[material_id -> 已用次数]
        max_uses: 每素材最大使用次数
        jaccard_target: Jaccard 软约束阈值
        base_score: 基础分（关键词匹配等由调用方传入）

    Returns:
        打分（0 表示不可用，base_score 表示完美）
    """
    used = usage_count.get(int(material_id), 0)
    if used >= max_uses:
        return 0.0  # 硬上限
    quota_left = (max_uses - used) / max_uses  # 0..1

    if not recent_sets:
        return base_score * (0.5 + 0.5 * quota_left)

    # 软约束：与最近 N 条的 Jaccard
    target_set = {int(material_id)}
    jaccards = [compute_jaccard(target_set, s) for s in recent_sets]
    avg_jacc = sum(jaccards) / len(jaccards)

    # avg_jacc 越高（越像最近一条），分越低
    jaccard_penalty = max(0.0, avg_jacc - jaccard_target) / max(1.0 - jaccard_target, 1e-9)
    jaccard_penalty = min(1.0, jaccard_penalty)

    return base_score * (0.5 + 0.5 * quota_left) * (1.0 - jaccard_penalty)
