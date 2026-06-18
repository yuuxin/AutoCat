"""TTS 配音 + 时间戳分句 + SRT 字幕生成

不依赖 Whisper ASR，直接从 TTS 输出获取每句时间戳。
"""

import json
import re
import asyncio
import subprocess
import tempfile
import time
import unicodedata
from pathlib import Path
from autokat.core.ffmpeg_utils import FFPROBE
from typing import Optional

import edge_tts

from autokat.models.db import get_conn
from autokat.core.paths import ASSETS_ROOT
from autokat.core.subtitle_sync import (
    MAX_CAPTION_CHARS, prepare_pcm_and_calibrate, semantic_unit_chunks,
    split_punctuation_clauses,
)

TTS_DIR = ASSETS_ROOT / "tts"
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
        # 中文：目标标点始终断句，标点保留在上一条字幕。
        raw = re.split(r'(?<=[，；：。！？\n])\s*', text)
        sentences = [s.strip() for s in raw if s.strip()]
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
    communicate = edge_tts.Communicate(
        text, voice=voice, rate=rate, pitch=pitch, boundary="WordBoundary"
    )
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


def _probe_audio_duration(output_path: str) -> Optional[float]:
    try:
        result = subprocess.run(
            [FFPROBE, "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", output_path],
            capture_output=True, text=True, timeout=30,
        )
        duration = float(result.stdout.strip())
        return duration if duration > 0 else None
    except Exception:
        return None


_ZH_BREAK_PUNCTUATION = set("，；：。！？")
_TTS_ATTEMPTS_PER_VOICE = 3


# ── TTS 文本预处理 (v3.21) ──
# 任务 568/753/760 报告: "第 4/5 段配音或字幕时间轴失败: TTS 在 9 次尝试后
# 仍失败: No audio was received." 根因是 edge-tts 对以下文本严格返回 NoAudioReceived:
#   - 空字符串 / 纯空白
#   - 净化后只剩 # @ ! * 等标牌/装饰字符
#   - 只含 emoji
# 旧代码直接把原文喂给 edge-tts, 9 次重试全部空跑, 既耗时长又无救.
# 修复: 入口处 sanitize + 短空段短路 + 分段回退.
_TTS_STRIP_CHARS = set(
    "#@*•·●○◎◇◆■□▲△▼▽★☆♀♂※→←↑↓"
    "【】[]()（）"
)


def _sanitize_for_tts(text: str) -> tuple[str, bool]:
    """把 TTS 文本清洗成 edge-tts 一定能产音频的形式.

    返回 (sanitized_text, ok). ok=False 表示净化后无可发音内容,
    上层应 raise 明确错误 (含原文本预览), 而不是空跑 9 次重试.
    """
    if not text:
        return "", False
    cleaned = []
    for ch in str(text):
        if ch in _TTS_STRIP_CHARS:
            cleaned.append(" ")
        else:
            cleaned.append(ch)
    s = "".join(cleaned)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return "", False
    # 验证可发音字符数 (中文/英文字母/数字, 至少 2 个)
    pronounceable = sum(
        1 for ch in s
        if "\u4e00" <= ch <= "\u9fff"
        or (ch.isascii() and (ch.isalpha() or ch.isdigit()))
    )
    if pronounceable < 2:
        return "", False
    return s, True


def _split_for_chunked_tts(text: str, max_chars: int = 600,
                           force_split: bool = False) -> list[str]:
    """把长文本切分成长度可控的子段, 用于整段 TTS 失败时的回退.

    按优先级切分: 。！？!?\\n > ,;:，；：
    每个子段 ≤ max_chars 字符, 至少 5 个字符 (避免 sanitize 后变空).
    """
    if len(text) <= max_chars and not force_split:
        return [text]
    chunks: list[str] = []
    primary = re.split(r"(?<=[。！？!?\n])", text)
    if force_split:
        forced_chunks: list[str] = []
        for piece in primary:
            piece = piece.strip()
            if not piece:
                continue
            if len(piece) <= max_chars:
                forced_chunks.append(piece)
            else:
                forced_chunks.extend(
                    _split_for_chunked_tts(
                        piece, max_chars=max_chars, force_split=False,
                    )
                )
        if len(forced_chunks) > 1:
            return [c for c in forced_chunks if len(c.strip()) >= 5]
    buf = ""
    for piece in primary:
        if not piece:
            continue
        if len(buf) + len(piece) <= max_chars:
            buf += piece
        else:
            if buf:
                chunks.append(buf)
            if len(piece) > max_chars:
                secondary = re.split(r"(?<=[，；：,;:])", piece)
                sub_buf = ""
                for sub in secondary:
                    if len(sub_buf) + len(sub) <= max_chars:
                        sub_buf += sub
                    else:
                        if sub_buf:
                            chunks.append(sub_buf)
                        sub_buf = sub
                if sub_buf:
                    chunks.append(sub_buf)
                buf = ""
            else:
                buf = piece
    if buf:
        chunks.append(buf)
    return [c for c in chunks if len(c.strip()) >= 5]


def _spoken_text(text: str) -> str:
    """Normalize text to characters Edge TTS can emit as WordBoundary content."""
    return "".join(
        char for char in str(text)
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    )


def _generate_narration_chunked(chunks: list[str],
                                voice: str, rate: str, pitch: str,
                                output_path: str, lang: str,
                                attempts_per_chunk: int = 3) -> dict:
    """整段 TTS 失败时的回退: 逐段 TTS 后 ffmpeg 拼接.

    Returns: 与 generate_narration 相同的 dict
    (含 audio_path / total_duration / sentences).
    """
    import os
    import subprocess
    from autokat.core.ffmpeg_utils import FFMPEG
    chunk_files: list[str] = []
    all_sentences: list[dict] = []
    current_offset = 0.0
    for idx, chunk in enumerate(chunks):
        chunk_path = output_path.replace(".mp3", f"_chunk{idx}.mp3")
        chunk_files.append(chunk_path)
        chunk_dur = None
        chunk_boundaries = []
        chunk_exc = None
        for attempt in range(1, attempts_per_chunk + 1):
            try:
                chunk_dur, chunk_boundaries = asyncio.run(
                    _generate_tts_with_boundaries(
                        chunk, chunk_path, voice, rate, pitch,
                    )
                )
                if chunk_dur and chunk_dur >= 0.1 and chunk_boundaries:
                    break
                raise RuntimeError(
                    f"音频或 WordBoundary 为空: duration={chunk_dur}, "
                    f"boundaries={len(chunk_boundaries)}"
                )
            except Exception as exc:
                chunk_exc = exc
                chunk_dur, chunk_boundaries = None, []
                if attempt < attempts_per_chunk:
                    time.sleep(float(attempt))
        if not chunk_dur or chunk_dur < 0.1:
            raise RuntimeError(
                f"chunk {idx+1}/{len(chunks)} TTS 在 "
                f"{attempts_per_chunk} 次尝试后失败: {chunk_exc or '无音频'}; "
                f"文本 {chunk[:30]!r}"
            ) from chunk_exc
        chunk_sentence_timings = build_phrase_timings(
            chunk_boundaries, source_text=chunk if lang == "zh" else None,
            max_chars=MAX_CAPTION_CHARS if lang == "zh" else 20,
        )
        for s in chunk_sentence_timings:
            s["start"] = s["start"] + current_offset
            s["end"] = s["end"] + current_offset
        all_sentences.extend(chunk_sentence_timings)
        current_offset += chunk_dur
    for sentence_index, sentence in enumerate(all_sentences):
        sentence["index"] = sentence_index
    concat_list = output_path + ".list.txt"
    with open(concat_list, "w") as f:
        for cf in chunk_files:
            f.write(f"file '{os.path.abspath(cf)}'\n")
    cmd = [FFMPEG, "-y", "-f", "concat", "-safe", "0",
           "-i", concat_list, "-c", "copy", output_path]
    concat_result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=60,
    )
    os.remove(concat_list)
    for cf in chunk_files:
        if os.path.exists(cf):
            os.remove(cf)
    if concat_result.returncode != 0 or not os.path.exists(output_path):
        raise RuntimeError(
            "TTS 分段音频拼接失败: "
            + (concat_result.stderr or "ffmpeg 未生成输出")[-300:]
        )
    total_duration = _probe_audio_duration(output_path) or current_offset
    if lang == "zh":
        output_path, all_sentences, total_duration = prepare_pcm_and_calibrate(
            output_path, all_sentences, total_duration,
        )
    return {
        "audio_path": output_path,
        "total_duration": total_duration,
        "sentences": all_sentences,
        "timing_source": (
            "word_boundary+pcm_vad" if lang == "zh" else "word_boundary"
        ),
    }


def _display_chunks(clause: str, chunks: list[list[dict]]) -> list[str]:
    """Restore all original punctuation while keeping spoken-unit chunk boundaries."""
    if len(chunks) <= 1:
        return [clause.strip()]
    ends = []
    cumulative = 0
    for chunk in chunks:
        cumulative += sum(unit["chars"] for unit in chunk)
        ends.append(cumulative)
    result = []
    start = 0
    spoken_count = 0
    end_index = 0
    for index, char in enumerate(clause):
        if _spoken_text(char):
            spoken_count += 1
        if end_index < len(ends) - 1 and spoken_count == ends[end_index]:
            next_index = index + 1
            while next_index < len(clause) and not _spoken_text(clause[next_index]):
                next_index += 1
            result.append(clause[start:next_index].strip())
            start = next_index
            end_index += 1
    result.append(clause[start:].strip())
    return result


def build_phrase_timings(boundaries: list[dict], source_text: Optional[str] = None,
                         max_chars: int = MAX_CAPTION_CHARS) -> list[dict]:
    """Build punctuation-first captions using only real TTS boundary times."""
    units = []
    for boundary in boundaries:
        spoken = _spoken_text(boundary.get("text", ""))
        if not spoken:
            continue
        units.append({
            "text": spoken,
            "chars": len(spoken),
            "start": float(boundary["start"]),
            "end": float(boundary["end"]),
        })
    if not units:
        raise ValueError("TTS 未返回可用 WordBoundary")

    if source_text is None:
        source_text = "".join(str(boundary.get("text", "")) for boundary in boundaries)
    source_spoken = _spoken_text(source_text)
    boundary_spoken = "".join(unit["text"] for unit in units)
    if source_spoken != boundary_spoken:
        raise ValueError(
            "WordBoundary 与原始文案无法完整对齐: "
            f"source={source_spoken!r}, boundaries={boundary_spoken!r}"
        )

    clauses = split_punctuation_clauses(source_text)
    phrases = []
    unit_index = 0
    for clause in clauses:
        wanted_chars = len(_spoken_text(clause))
        clause_units = []
        consumed = 0
        while unit_index < len(units) and consumed < wanted_chars:
            unit = units[unit_index]
            if consumed + unit["chars"] > wanted_chars:
                raise ValueError(f"WordBoundary 跨越标点断句边界: {clause!r}")
            clause_units.append(unit)
            consumed += unit["chars"]
            unit_index += 1
        if consumed != wanted_chars:
            raise ValueError(f"WordBoundary 未覆盖完整标点短句: {clause!r}")

        chunks = semantic_unit_chunks(clause_units, clause, max_chars=max_chars)
        display_chunks = _display_chunks(clause, chunks)
        for chunk_index, chunk in enumerate(chunks):
            phrases.append({
                "index": len(phrases),
                "text": display_chunks[chunk_index],
                "start": chunk[0]["start"],
                "end": chunk[-1]["end"],
                "timing_source": "word_boundary",
            })

    if unit_index != len(units):
        raise ValueError("存在未映射到字幕的 WordBoundary")
    for index, phrase in enumerate(phrases):
        phrase["index"] = index
        phrase["start"] = round(phrase["start"], 6)
        phrase["end"] = round(phrase["end"], 6)
    return phrases


async def _generate_tts_with_boundaries(text: str, output_path: str,
                                        voice: str, rate: str,
                                        pitch: str) -> tuple[Optional[float], list[dict]]:
    """Generate one continuous audio file and collect real WordBoundary times."""
    communicate = edge_tts.Communicate(
        text, voice=voice, rate=rate, pitch=pitch, boundary="WordBoundary"
    )
    boundaries = []
    with open(output_path, "wb") as audio_file:
        async for chunk in communicate.stream():
            chunk_type = chunk.get("type")
            if chunk_type == "audio":
                audio_file.write(chunk["data"])
            elif chunk_type == "WordBoundary":
                start = float(chunk.get("offset", 0)) / 10_000_000
                duration = float(chunk.get("duration", 0)) / 10_000_000
                boundaries.append({
                    "text": chunk.get("text", ""),
                    "start": start,
                    "end": start + duration,
                })
    return _probe_audio_duration(output_path), boundaries


def _fallback_sentence_timings(text: str, duration: float, lang: str) -> list[dict]:
    sentences = split_sentences(text, lang=lang)
    if not sentences or duration <= 0:
        return []
    weights = [max(1, len(sentence.replace(" ", ""))) for sentence in sentences]
    total_weight = sum(weights)
    current = 0.0
    timings = []
    for index, (sentence, weight) in enumerate(zip(sentences, weights)):
        end = duration if index == len(sentences) - 1 else current + duration * weight / total_weight
        timings.append({
            "index": index,
            "text": sentence,
            "start": round(current, 3),
            "end": round(end, 3),
        })
        current = end
    return timings


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
    # v3.21: 入口处 sanitize, 避免把空 / 纯符号 / 纯 emoji 喂给 edge-tts
    # (任务 568 报告: 第 4/5 段被净化为空后 9 次重试全部 NoAudioReceived)
    sanitized_text, _ok = _sanitize_for_tts(text)
    if not _ok:
        raise ValueError(
            f"TTS 文本无可发音内容 (原文本长度 {len(text)} 字符, "
            f"预览: {text[:50]!r}). 请检查文案是否被 # @ * 等装饰字符占满, "
            f"或文案本身为空."
        )
    text = sanitized_text

    # 根据语言选默认 TTS 配置
    cfg = LANG_CONFIG.get(lang, LANG_CONFIG["zh"])
    if voice is None:
        voice = cfg["voice"]
    if rate is None:
        rate = cfg["rate"]
    if pitch is None:
        pitch = cfg["pitch"]

    if output_name:
        audio_path = str(TTS_DIR / f"{output_name}.mp3")
    else:
        # 用内容 hash 做文件名
        import hashlib
        h = hashlib.md5(text.encode()).hexdigest()[:12]
        audio_path = str(TTS_DIR / f"narration_{h}.mp3")

    # 同语言备用语音列表（按可靠性排序）
    _fallback_voices = {
        "zh": ["zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural", "zh-CN-XiaohanNeural"],
        "th": ["th-TH-PremwadeeNeural", "th-TH-NiwatNeural"],
    }
    _lang_voices = _fallback_voices.get(lang, [])
    # 构建去重尝试列表：首选 → 同语言备选
    _candidates = []
    for v in [voice] + _lang_voices:
        if v not in _candidates:
            _candidates.append(v)

    total_duration = None
    word_boundaries = []
    sentence_timings = []
    last_exc = None
    for candidate in _candidates:
        for attempt in range(1, _TTS_ATTEMPTS_PER_VOICE + 1):
            try:
                total_duration, word_boundaries = asyncio.run(
                    _generate_tts_with_boundaries(text, audio_path, candidate, rate, pitch)
                )
                if not total_duration or total_duration < 0.1:
                    raise RuntimeError(f"音频时长异常: {total_duration}")
                if not word_boundaries:
                    raise RuntimeError("TTS 未返回 WordBoundary")
                sentence_timings = build_phrase_timings(
                    word_boundaries,
                    source_text=text if lang == "zh" else None,
                    max_chars=MAX_CAPTION_CHARS if lang == "zh" else 20,
                )
                break
            except Exception as exc:
                last_exc = exc
                total_duration, word_boundaries, sentence_timings = None, [], []
                print(
                    f"[TTS] Voice {candidate} attempt "
                    f"{attempt}/{_TTS_ATTEMPTS_PER_VOICE} failed: {exc}"
                )
                if attempt < _TTS_ATTEMPTS_PER_VOICE:
                    time.sleep(0.5 * attempt)
        if sentence_timings:
            break

    if not total_duration or total_duration < 0.1:
        # 整段请求失败不一定是文本过长。Edge TTS 短时服务异常或单次请求
        # 被拒绝时，几十到一百字的正常短文也可能连续返回 NoAudioReceived。
        # 冷却后强制按标点拆成小请求，既绕开内容边界，也避开原重试窗口。
        chunks = _split_for_chunked_tts(
            text, max_chars=80, force_split=True,
        ) or [text]
        try:
            time.sleep(3.0)
            print(
                f"[TTS] 整段重试失败, 冷却后切分为 "
                f"{len(chunks)} 段重试…"
            )
            return _generate_narration_chunked(
                chunks, voice, rate, pitch, audio_path, lang,
            )
        except Exception as fallback_exc:
            print(f"[TTS] chunked 回退也失败: {fallback_exc}")
        raise RuntimeError(
            f"TTS 在 {len(_candidates) * _TTS_ATTEMPTS_PER_VOICE} 次尝试后仍失败: "
            f"{last_exc or 'unknown error'}. 文本预览: {text[:60]!r} "
            f"(sanitize 后长度 {len(text)} 字符)"
        ) from last_exc
    if lang == "zh":
        try:
            audio_path, sentence_timings, total_duration = prepare_pcm_and_calibrate(
                audio_path, sentence_timings, total_duration,
            )
        except Exception as exc:
            raise RuntimeError(f"PCM/VAD 字幕校准失败: {exc}") from exc
    if on_sentence is not None:
        for index, timing in enumerate(sentence_timings, 1):
            try:
                on_sentence(index, len(sentence_timings), timing["text"])
            except Exception:
                pass

    return {
        "audio_path": audio_path,
        "total_duration": round(total_duration, 3),
        "sentences": sentence_timings,
        "timing_source": (
            "word_boundary+pcm_vad"
            if lang == "zh" else ("word_boundary" if word_boundaries else "estimated")
        ),
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
