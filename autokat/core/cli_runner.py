"""CLI/GUI 共用的生成入口

所有"生成视频"的操作都走这里，确保：
- 后台 CLI 调用 `autokat generate` 跟 GUI 点「开始生成」产生**完全一致**的输出
- 唯一差异仅是 UI 反馈（log 写入 QTextEdit vs print）
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from autokat.models.db import init_db
from autokat.core.tts import save_script, list_scripts
from autokat.models.db import get_conn
from autokat.core.renderer import create_and_run_batch
from autokat.core.bgm import pick_random_bgm


def _get_or_create_script(name: str, narration: str, lang: str, tts_config=None) -> int:
    """GUI 复用：找同名 script 行复用，否则新建

    逻辑：用 (name, narration[:50]) 哈希匹配，narration 变了就新建
    """
    conn = get_conn()
    nar_hash = hash(narration[:50]) if narration else 0
    row = conn.execute(
        "SELECT id FROM scripts WHERE name=? ORDER BY id DESC LIMIT 5", (name,)
    ).fetchall()
    for r in row:
        # 简单复用：name 相同且 narration 前 50 字符一样
        old = conn.execute("SELECT narration FROM scripts WHERE id=?", (r["id"],)).fetchone()
        if old and (hash(old["narration"][:50]) if old["narration"] else 0) == nar_hash:
            conn.close()
            # 顺便更新 tts_config（如果传了）
            if tts_config:
                conn2 = get_conn()
                conn2.execute("UPDATE scripts SET tts_config=? WHERE id=?", (json.dumps(tts_config), r["id"]))
                conn2.commit()
                conn2.close()
            return r["id"]
    conn.close()
    return save_script(name, narration, lang=lang, tts_config=tts_config)


def _pick_default_bgm() -> Optional[str]:
    """CLI 没有指定 BGM 时，自动挑一个 assets/bgm/ 下的文件"""
    return pick_random_bgm()


def _parse_rate(s) -> str:
    """--rate -5  → '+0%' 格式（不依赖 str.format 的 + 号）"""
    s = str(s).strip().replace("%", "")
    sign = "+" if not s.startswith("-") else ""
    return f"{sign}{s}%"


def _parse_pitch(s) -> str:
    s = str(s).strip().replace("Hz", "").replace("hz", "")
    sign = "+" if not s.startswith("-") else ""
    return f"{sign}{s}Hz"


def run_generate(
    text: str,
    name: str = "CLI生成",
    *,
    # 计数器 & 资源
    count: int = 100,
    workers: int = 2,
    fps: int = 30,
    # 语言 & TTS
    lang: str = "zh",
    voice: Optional[str] = None,
    rate: Optional[str] = None,
    pitch: Optional[str] = None,
    # 编排参数
    min_shot_duration: float = 2.0,
    # 字幕 / 差异化
    subtitle_position: Optional[str] = None,  # 字幕位置（"底部"等），None = 随机
    shuffle: bool = True,                    # 随机打乱素材顺序
    enable_transition: bool = True,           # 启用随机转场
    # enable_color_filter 已移除: v2.3 简化, 混剪过程不做调色
    # BGM
    no_bgm: bool = False,
    bgm: Optional[str] = None,
    bgm_files: Optional[list[str]] = None,  # 多BGM文件列表
    # 素材
    materials: Optional[list] = None,
    # v2.3 差异化（GUI 用：把新 UI 字段打包成 dict 透传）
    extra_config: Optional[dict] = None,
    # 行为
    reuse_script: bool = False,
    wait: bool = True,
    log_fn = print,
) -> int:
    """CLI/GUI 共用的生成入口

    Args:
        text: 口播文案（多段用 --- 分隔，每段独立 TTS+独立视频）
        name: 脚本名（CLI 默认 "CLI生成"，GUI 用 wizard_draft["script_name"]）
        count: 生成视频数量
        workers: 并发进程数
        fps: 帧率（30/60）
        lang: 语言 (zh/th/en)
        voice: TTS 音色（如 th-TH-PremwadeeNeural）；None 用 LANG_CONFIG 默认
        rate: 语速 -50..+50（如 -5）
        pitch: 音调 -50..+50
        min_shot_duration: 每段最短秒数
        no_bgm: 不用 BGM
        bgm: 指定 BGM 路径
        materials: 限定素材 id 列表（None = 全部）
        extra_config: v2.3 透传差异化配置 dict（platform/perturbation_level/
            max_uses_per_slice/enable_diversity/dedup_threshold 等）。
            键会 merge 到 batch_config，传给 editor/renderer。
        reuse_script: True 复用同名 script（GUI 默认），False 每次新建（CLI 默认）
        wait: True 阻塞等渲染完，False 立即返回 task_id
        log_fn: 日志函数（CLI 走 print，GUI 走 _wiz_log.append）
    """
    init_db()

    # 1) 构造 narration_config（GUI / CLI 走完全一样的格式）
    tts_config = {}
    if voice:
        tts_config["voice"] = voice
    if rate is not None:
        tts_config["rate"] = _parse_rate(rate)
    if pitch is not None:
        tts_config["pitch"] = _parse_pitch(pitch)

    # 2) BGM 处理
    if no_bgm:
        enable_bgm = False
        bgm_path = None
    elif bgm:
        enable_bgm = True
        bgm_path = str(bgm)
    else:
        enable_bgm = True
        bgm_path = _pick_default_bgm()
        if not bgm_path:
            log_fn("[BGM] 找不到默认 BGM，自动禁用")
            enable_bgm = False

    # 3) 素材 ids（CLI 逗号分隔字符串 → list）
    if isinstance(materials, str):
        materials = [int(x) for x in materials.split(",") if x.strip()]
    elif materials is None:
        materials = None  # None = 全部

    # 4) 构造 batch config（GUI / CLI 一致）
    # v2.3 增强：extra_config 透传新字段（platform/perturbation_level/max_uses_per_slice 等）
    batch_config = {
        "min_shot_duration": float(min_shot_duration),
    }
    if extra_config:
        batch_config.update(extra_config)

    # 5) 脚本入库
    if reuse_script:
        # GUI 默认行为：复用同名 script 行
        script_id = _get_or_create_script(name, text, lang=lang, tts_config=tts_config or None)
    else:
        # CLI 默认行为：每次新建一条 script 记录
        script_id = save_script(name, text, lang=lang, tts_config=tts_config or None)


    # 6) 实际生成（核心调用，GUI/CLI 共用同一条路径）
    task_id = create_and_run_batch(
        script_id=script_id,
        narration_text=text,
        narration_config=tts_config or None,
        count=int(count),
        workers=int(workers),
        fps=int(fps),
        enable_bgm=enable_bgm,
        bgm_files=bgm_files,
        bgm_path=bgm_path,
        lang=lang,
        material_ids=materials,
        config=batch_config,
        subtitle_position=subtitle_position,
        log_fn=log_fn,
    )
    log_fn(f"[run_generate] 任务已创建: task_id={task_id}")
    return task_id
