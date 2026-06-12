#!/usr/bin/env python3
"""Generate the reported five-script scenario and write quality validation logs."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autokat.core.writer import (
    estimate_chars_for_duration_range, generate_script_by_topic_detailed,
    validate_script_quality,
)


def main() -> None:
    topic, style, lang = "时尚女鞋", "种草推荐", "zh"
    count, duration_min, duration_max, rate = 5, 25, 30, 0
    target_min, target_max = estimate_chars_for_duration_range(
        lang, duration_min, duration_max, rate,
    )
    accepted = []
    records = []

    def progress(backend, attempt, total, message):
        print(f"[生成] {backend} {attempt}/{total}: {message}", flush=True)

    for index in range(count):
        generated = generate_script_by_topic_detailed(
            topic, style, lang=lang,
            extra_instruction=f"第{index + 1}条，目标时长{duration_min}-{duration_max}秒",
            target_chars_min=target_min, target_chars_max=target_max,
            accepted_texts=accepted, progress_callback=progress,
        )
        accepted.append(generated["text"])
        quality = validate_script_quality(
            generated["text"], topic, lang=lang,
            target_chars_min=target_min, target_chars_max=target_max,
            accepted_texts=accepted[:-1],
        )
        records.append({
            "index": index + 1, "source": generated["source"],
            "text": generated["text"], "quality": quality,
            "estimated_duration": quality["char_count"] / 4.76,
        })
        print(
            f"[通过] 第{index + 1}条 source={generated['source']} "
            f"chars={quality['char_count']} estimated={records[-1]['estimated_duration']:.2f}s "
            f"similarity={quality['max_similarity']:.2f}",
            flush=True,
        )

    passed = len(records) == count and all(record["quality"]["valid"] for record in records)
    output_dir = ROOT / "outputs" / "writer_quality_validation"
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "passed": passed, "topic": topic, "style": style, "lang": lang,
        "duration_range": [duration_min, duration_max],
        "target_chars": [target_min, target_max], "records": records,
    }
    (output_dir / "writer_quality_validation.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    lines = [
        "# AI 批量文案质量校验报告", "",
        f"结果：**{'PASS' if passed else 'FAIL'}**",
        f"选题：{topic}；风格：{style}；目标：{count} 条 / {duration_min}-{duration_max}s / {target_min}-{target_max} 字符",
        "",
    ]
    for record in records:
        quality = record["quality"]
        lines += [
            f"## 第 {record['index']} 条",
            f"- 来源：{record['source']}",
            f"- 字符数：{quality['char_count']}",
            f"- 预计时长：{record['estimated_duration']:.2f}s",
            f"- 最大批次相似度：{quality['max_similarity']:.2f}",
            f"- 校验：{'PASS' if quality['valid'] else 'FAIL'}",
            "",
            record["text"],
            "",
        ]
    (output_dir / "writer_quality_validation.md").write_text("\n".join(lines), encoding="utf-8")
    print(output_dir / "writer_quality_validation.md")
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
