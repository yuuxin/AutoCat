"""TTS 配音 + 时间戳分句 + SRT 字幕生成

不依赖 Whisper ASR，直接从 TTS 输出获取每句时间戳。
"""

import json
import re
import asyncio
import subprocess
import tempfile
from pathlib import Path
from autokat.core.ffmpeg_utils import FFPROBE
from typing import Optional

import edge_tts

from autokat.models.db import get_conn

TTS_DIR = Path(__file__).resolve().parent.parent.parent / "assets" / "tts"
TTS_DIR.mkdir(parents=True, exist_ok=True)


# ── 语言配置 ──

# ── v2.3 13 音色池 (zh×5 + en×5 + th×3) ──
# 矩阵号去重 (画面同质化 + 声纹同质化) 核心: 同一脚本 13 个音色随机选, 语速/音调也 jitter
# 13 是覆盖口播+评测主流的最常用组合, Edge-TTS 全部可商用
LANG_CONFIG = {
    "zh": {
        "voice": "zh-CN-XiaoxiaoNeural",
        "rate": "+0%",
        "pitch": "+0Hz",
        "font": "Source Han Sans",
        "voice_options": [
            "zh-CN-XiaoxiaoNeural",   # 女 · 通用
            "zh-CN-YunxiNeural",      # 男 · 年轻
            "zh-CN-YunjianNeural",    # 男 · 新闻
            "zh-CN-XiaoyiNeural",     # 女 · 温柔
            "zh-CN-YunyangNeural",    # 男 · 专业
        ],
    },
    "en": {
        "voice": "en-US-JennyNeural",
        "rate": "+0%",
        "pitch": "+0Hz",
        "font": "Arial",
        "voice_options": [
            "en-US-JennyNeural",      # 女 · 通用
            "en-US-GuyNeural",        # 男 · 年轻
            "en-US-AriaNeural",       # 女 · 自然
            "en-US-DavisNeural",      # 男 · 商务
            "en-US-SaraNeural",       # 女 · 童声
        ],
    },
    "th": {
        "voice": "th-TH-PremwadeeNeural",
        "rate": "-5%",
        "pitch": "+0Hz",
        "font": "Thonburi",
        "voice_options": [
            "th-TH-PremwadeeNeural",  # 女 · 通用
            "th-TH-NiwatNeural",      # 男 · 通用
            "th-TH-AcharaNeural",     # 女 · 自然
        ],
    },
}


ALL_VOICES: list[str] = [
    v for cfg in LANG_CONFIG.values() for v in cfg["voice_options"]
]


def voice_choices_for(lang: Optional[str]) -> list[str]:
    """返回某语言可用的音色列表, lang 不在表里就返回 ALL_VOICES。"""
    if not lang:
        return list(ALL_VOICES)
    cfg = LANG_CONFIG.get(lang)
    if not cfg:
        return list(ALL_VOICES)
    return list(cfg["voice_options"])


# ── 分句规则 ──

def split_sentences(text: str, lang: str = "zh") -> list[str]:
    """按标点符号将文案切分为短句

    中文：按句号/问号/感叹号/逗号 切分
    泰文：按句号/空格/换行 切分（泰文以空格为词边界）
    """
    if lang == "th":
        # 泰文：按 . ? ! 和换行切分（不使用中文标点）
        raw = re.split(r'(?<=[.?!\n])\s*', text)
        sentences = []
        for s in raw:
            s = s.strip()
            if not s:
                continue
            if len(s) > 40:
                # 泰文长句按空格再分（每个子句至少保留 5 个字符）
                parts = re.split(r'(?<= )', s)
                buf = ""
                for p in parts:
                    if len(buf + p) <= 50:
                        buf += p
                    else:
                        if buf.strip():
                            sentences.append(buf.strip())
                        buf = p
                if buf.strip():
                    sentences.append(buf.strip())
            else:
                sentences.append(s)
    else:
        # 中文：按句号、问号、感叹号、分句、换行切分
        raw = re.split(r'(?<=[。！？\n])\s*', text)
        sentences = []
        for s in raw:
            s = s.strip()
            if not s:
                continue
            # 如果句子超过 30 个字，按逗号再分
            if len(s) > 30:
                sub = re.split(r'(?<=[，；、])\s*', s)
                sentences.extend([x.strip() for x in sub if x.strip()])
            else:
                sentences.append(s)
    return sentences


# ── 配音生成 ──

async def _generate_tts(text: str, output_path: str,
                        voice: str = "zh-CN-XiaoxiaoNeural",
                        rate: str = "+0%",
                        pitch: str = "+0Hz") -> Optional[float]:
    """使用 Edge-TTS 生成配音，返回音频时长(秒)
    
    如果 Edge-TTS 失败（网络/语音不匹配），会抛出异常由调用方处理回退。
    """
    import os
    communicate = edge_tts.Communicate(text, voice=voice, rate=rate, pitch=pitch)
    await communicate.save(output_path)

    # 用 ffprobe 获取时长
    cmd = [
        FFPROBE, "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        output_path
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        dur = float(result.stdout.strip())
        if dur > 0:
            return dur
    except Exception:
        pass
    # fallback: estimate duration from file size (mp3 ~16kbps)
    try:
        size = os.path.getsize(output_path)
        if size > 1000:
            return max(1.0, size / 2000.0)
    except Exception:
        pass
    return None


def generate_narration(text: str,
                       voice: Optional[str] = None,
                       rate: Optional[str] = None,
                       pitch: Optional[str] = None,
                       output_name: Optional[str] = None,
                       lang: str = "zh",
                       on_sentence=None) -> Optional[dict]:
    """生成配音，返回分句时间轴

    Args:
        ... (原有参数)
        on_sentence: 可选回调，签名 (done: int, total: int, sentence: str) -> None
            每完成一句配音后调用一次（失败重试不会重复触发），
            供 Step 4 的"当前活动"标签实时刷句进度，避免 30s+ 的 TTS 期间界面静止。

    Returns:
        {
            "audio_path": str,
            "total_duration": float,
            "sentences": [
                {"index": 0, "text": "...", "start": 0.0, "end": 2.5},
                ...
            ],
        }
    """
    # 根据语言选默认 TTS 配置
    cfg = LANG_CONFIG.get(lang, LANG_CONFIG["zh"])
    if voice is None:
        voice = cfg["voice"]
    if rate is None:
        rate = cfg["rate"]
    if pitch is None:
        pitch = cfg["pitch"]

    sentences = split_sentences(text, lang=lang)

    if output_name:
        audio_path = str(TTS_DIR / f"{output_name}.mp3")
    else:
        # 用内容 hash 做文件名
        import hashlib
        h = hashlib.md5(text.encode()).hexdigest()[:12]
        audio_path = str(TTS_DIR / f"narration_{h}.mp3")

    # 整体生成配音（Edge-TTS 支持 SSML，但不支持逐句精准时间戳）
    # 方案：逐句生成，用 ffmpeg 拼接，记录每句起止时间
    seg_dir = TTS_DIR / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)

    sentence_timings = []
    seg_files = []
    current_time = 0.0

    import hashlib
    h = hashlib.md5(text.encode()).hexdigest()[:12]

    # 尝试首选语音，失败时回退（梯度尝试：首选 → 语言默认 → 中文语音兜底）
    _voice = voice
    _voice_fallback = cfg["voice"]
    # 尝试首选语音，失败时回退到同语言的其他语音
    _voice = voice
    # 同语言备用语音列表（按可靠性排序）
    _fallback_voices = {
        "zh": ["zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural", "zh-CN-XiaohanNeural"],
        "th": ["th-TH-PremwadeeNeural", "th-TH-NiwatNeural"],
    }
    _lang_voices = _fallback_voices.get(lang, [])
    # 构建去重尝试列表：首选 → 同语言备选
    _candidates = []
    for v in [_voice] + _lang_voices:
        if v not in _candidates:
            _candidates.append(v)

    for i, sent in enumerate(sentences):
        if not sent.strip():
            continue
        seg_path = str(seg_dir / f"{h}_seg{i:03d}.mp3")
        dur = None
        last_exc = None
        for cv in _candidates:
            try:
                dur = asyncio.run(_generate_tts(sent, seg_path, cv, rate, pitch))
                if dur is not None and dur >= 0.1:
                    break
                err_msg = f"Audio too short ({dur}s)" if dur is not None else "Returned None"
                print(f"[TTS] Voice {cv} problem: {err_msg}")
            except Exception as e:
                print(f"[TTS] Voice {cv} failed: {e}")
                last_exc = e
            dur = None
        if dur is None or dur < 0.1:
            # 所有语音都失败了，记录具体的失败原因
            err_detail = str(last_exc) if last_exc else "Unknown error"
            print(f"[TTS] All voices failed for sentence {i}: {err_detail}")
            continue
            continue

        sentence_timings.append({
            "index": i,
            "text": sent,
            "start": round(current_time, 3),
            "end": round(current_time + dur, 3),
        })
        seg_files.append(seg_path)
        current_time += dur
        # 逐句进度回调：调用方传了 on_sentence 就通知它 "第 i+1 句完成"，
        # 让 UI 端的"当前活动"标签持续刷新，避免 TTS 期间界面静止
        if on_sentence is not None:
            try:
                on_sentence(i + 1, len(sentences), sent)
            except Exception:
                # 回调失败不影响 TTS 主体
                pass

    if not seg_files:
        return None

    # 拼接所有音频片段
    filelist_path = str(seg_dir / f"{h}_filelist.txt")
    with open(filelist_path, "w") as f:
        for sf in seg_files:
            f.write(f"file '{sf}'\n")

    from autokat.core.ffmpeg_utils import FFMPEG as _FF
    cmd = [
        _FF, "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", filelist_path,
        "-c", "copy",
        audio_path
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    except subprocess.CalledProcessError as e:
        print(f"[TTS 拼接失败] {e.stderr.decode(errors='replace')[:200]}")
        # 如果拼接失败，直接用最后一段单独生成一个完整音频
        asyncio.run(_generate_tts(text, audio_path, voice, rate, pitch))
        # 此时没有逐句时间戳，按总时长均匀分句
        total_dur = current_time
        sentence_timings = []
        avg_dur = total_dur / len(sentences)
        for i, sent in enumerate(sentences):
            sentence_timings.append({
                "index": i,
                "text": sent,
                "start": round(i * avg_dur, 3),
                "end": round((i + 1) * avg_dur, 3),
            })

    total_duration = sentence_timings[-1]["end"] if sentence_timings else 0

    return {
        "audio_path": audio_path,
        "total_duration": total_duration,
        "sentences": sentence_timings,
    }


# ── SRT 字幕生成 ──

def _format_srt_time(seconds: float) -> str:
    """将秒数转为 SRT 时间格式 HH:MM:SS,mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def generate_srt(sentences: list[dict], output_path: str):
    """根据时间轴生成 SRT 字幕文件"""
    with open(output_path, "w", encoding="utf-8") as f:
        for i, sent in enumerate(sentences, 1):
            start = _format_srt_time(sent["start"])
            end = _format_srt_time(sent["end"])
            f.write(f"{i}\n{start} --> {end}\n{sent['text']}\n\n")
    return output_path


# ── 文案管理 ──

def save_script(name: str, narration: str, lang: str = "zh-CN",
                tts_config: Optional[dict] = None) -> int:
    """保存文案到数据库"""
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO scripts (name, narration, lang, tts_config) VALUES (?,?,?,?)",
        (name, narration, lang, json.dumps(tts_config or {}))
    )
    conn.commit()
    script_id = cur.lastrowid
    conn.close()
    return script_id


def get_script(script_id: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM scripts WHERE id=?", (script_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_scripts() -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM scripts ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]
