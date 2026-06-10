"""AI 文案生成模块 — 双模式：本地 Qwen + DeepSeek API（选配）

功能：
1. 根据选题/关键词自动生成短视频口播文案
2. 支持多风格（种草、教程、测评、故事等）
3. 默认本地 Qwen-0.5B 离线运行
4. 配置 DeepSeek API Key 后自动切换云端模式

硬件要求（本地模式）：
- 最低 4GB 内存即可运行
- M 芯片 Mac 可用 MPS 加速
- 首次加载约 10s，需下载 ~1GB 模型
"""

import os
import re
import json


# ── 按语速+语言+目标时长算预计文案字数 ──
# 基础朗读速度（字符/秒，实测 2026-06-08，n≥4 自然句子平均）
# 注意：en 为字母数，非字数；en 句子较稀疏，同时长所需字符数远多于 zh
_BASE_CHARS_PER_SEC = {
    "zh": 4.76,  # 中文 XiaoxiaoNeural 实测 ~4.8 字/秒
    "th": 12.50, # 泰文 PremwadeeNeural 实测 ~12.5 字/秒
    "en": 14.55, # 英文 JennyNeural 实测 ~14.6 字母/秒
}


def estimate_chars_for_lang(
    lang: str,
    duration_sec: float,
    rate_pct: int = 0,
    margin: float = 0.10,
) -> tuple:
    """根据语言、目标时长、语速，估算文案字数范围

    Args:
        lang: "zh" | "th" | "en"
        duration_sec: 目标时长（秒）
        rate_pct: edge-tts rate（-50~+50），负数=慢读，正数=快读
        margin: 容差，默认 ±10%

    Returns:
        (chars_min, chars_max, chars_ideal) 三元组
    """
    base = _BASE_CHARS_PER_SEC.get(lang, 4.0)
    # rate="+50%" → 速度×1.5；rate="-50%" → 速度×0.5
    eff_cps = base * (1 + rate_pct / 100.0)
    ideal = max(1, int(duration_sec * eff_cps))
    chars_min = max(1, int(ideal * (1 - margin)))
    chars_max = int(ideal * (1 + margin))
    return chars_min, chars_max, ideal

from pathlib import Path
from typing import Optional

# ── API 配置（通过环境变量设置） ──
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"

# ── 文案风格模板 ──

STYLES = {
    "种草推荐": {
        "prompt": "你是一个短视频带货博主，擅长用简短有力的语言推荐好物。",
        "template": "家人们，今天给大家安利一个宝藏好物，真的太香了！{topic}最大的亮点就是{topic_detail}，我已经用了{topic_features}，体验感直接拉满。价格也很良心，喜欢的姐妹赶紧冲！",
    },
    "生活技巧": {
        "prompt": "你是一个生活达人，擅长分享实用的生活小技巧。",
        "template": "今天教大家一个实用的小技巧，关于{topic}。很多人不知道，其实{topic_detail}，只需要{topic_features}就能轻松解决。学会这个技巧，生活效率提升一倍！记得点赞收藏哦。",
    },
    "知识科普": {
        "prompt": "你是一个知识博主，擅长用通俗易懂的方式讲解专业知识。",
        "template": "你知道吗？关于{topic}，很多人都理解错了。其实{topic_detail}，这件事的关键在于{topic_features}。看完这个视频，你就能彻底搞懂了。关注我，每天带你涨知识。",
    },
    "测评对比": {
        "prompt": "你是一个数码/好物测评博主，观点客观公正。",
        "template": "{topic}到底值不值得买？我用了两周，今天来跟大家说说真实体验。先说优点：{topic_detail}。再说缺点：{topic_features}。总的来说，如果你需要{topic_detail}，可以入。",
    },
    "励志感悟": {
        "prompt": "你是一个情感博主，擅长讲述温暖有力量的故事和感悟。",
        "template": "其实{topic}这件事，让我明白了{topic_detail}。以前我也觉得{topic_features}，但现在我懂了。分享给你们，希望能给你一些力量。愿你也能找到属于自己的光。",
    },
}


# ── 多样性角度轮盘（每次调用循环一个，确保 batch 不雷同）──
# ── 角度轮盘: 纯描述, 禁止任何【xxx式】元描述 (否则 AI 会学到污染输出) ──
_ANGLES = [
    "反差/震惊开场, 范例: 真没想到…, 万万没想到…",
    "限时/稀缺/立刻行动驱动, 范例: 再不下手就没了, 错过等一年",
    "先描述用户痛点场景, 再给解决方案, 制造代入感",
    "摆数据/对比/测评, 客观冷静有逻辑, 适合专业人群",
    "用反问/段子/调侃/自嘲, 营造轻松幽默氛围",
    "用一个生活小片段或人物切入, 故事感代入",
    "连续抛问题引导思考, 最后揭晓答案",
    "旧 vs 新 / 没用 vs 用了, 制造落差对比",
]


def _build_prompt(topic: str, style: str,
                  detail: Optional[str] = None,
                  features: Optional[str] = None,
                  lang: str = "zh",
                  extra_instruction: Optional[str] = None,
                  variation_index: int = 0,
                  target_chars_min: Optional[int] = None,
                  target_chars_max: Optional[int] = None) -> str:
    """构建 LLM prompt

    Args:
        topic: 选题
        style: 文案风格
        detail: 细节（可选）
        features: 卖点（可选）
        lang: 输出语言
        extra_instruction: 额外指令
        variation_index: 用于在 batch 中循环选择不同角度/句式
        target_chars_min/max: 目标字数范围（硬约束）
    """
    lang_map = {"zh": "中文", "th": "泰文", "en": "英文"}
    lang_text = lang_map.get(lang, "中文")
    lang_hint = f"请使用{lang_text}输出。" if lang != "zh" else ""
    extra_hint = extra_instruction or ""
    style_info = STYLES.get(style, STYLES["生活技巧"])
    has_detail = bool(detail and detail.strip())
    has_features = bool(features and features.strip())

    # 核心：detail/features 为空时，不强行塞占位符进 prompt
    if has_detail and has_features:
        template = style_info["template"].format(
            topic=topic, topic_detail=detail, topic_features=features,
        )
        template_block = f"模板参考：\n{template}"
    elif has_detail:
        # 只有细节：只引用 detail，不引用 features
        tpl = style_info["template"].replace("，{topic_features}", "").replace("{topic_features}，", "")
        template = tpl.format(topic=topic, topic_detail=detail, topic_features=detail)
        template_block = f"模板参考：\n{template}"
    elif has_features:
        # 只有卖点：只引用 features，不引用 detail
        tpl = style_info["template"].replace("，{topic_detail}", "").replace("{topic_detail}，", "")
        template = tpl.format(topic=topic, topic_detail=features, topic_features=features)
        template_block = f"模板参考：\n{template}"
    else:
        # 都没填：完全不引用占位符，让 AI 围绕 topic 自由发挥
        template_block = (
            f"围绕「{topic}」自由创作一段 30-60 秒的口播文案。\n"
            f"不要使用任何占位符（如【】、{{}}、xx、XX），所有内容必须围绕 {topic} 写实。"
        )

    # ── 多样性约束：每条文案的开场句式 + 情绪角度必须不同 ──
    angle = _ANGLES[variation_index % len(_ANGLES)]
    diversity_hint = (
        f"\n【多样性硬约束】\n"
        f"- 本次文案使用以下角度撰写: {angle}。\n"
        f"- 每次生成必须使用完全不同的开场句式和情绪角度, 禁止套用'姐妹们''家人们''朋友们'等高频套话开场(除非该角度本身强烈要求)。\n"
        f"- 句式、过渡词、收尾都必须与同批次的其他文案明显不同, 避免'真的太香了''直接拉满''绝绝子'等模板化表达。\n"
        f"- **禁止复读机**: 同一句话、同一短语、同一关键词在文中出现次数 <=1 次。"
        f"  尤其严禁连续堆叠重复短语, 如'限时抢购！限时特惠！限时优惠！''绝绝子！绝绝子！'这种复读机输出属于严重错误, 必须拆散或改写。\n"
        f"- **促销词汇控量**: '限时'、'抢购'、'特价'、'特惠'、'优惠'、'秒杀'、'直降'、'折扣' 等促销词单条文案中总出现次数 <=1 次,"
        f"  且不能与'立刻''马上''错过'等催促词堆叠出现。\n"
        f"- **数字不重复**: 同一数字、同一价格区间、同一百分比只能出现一次。\n"
    )

    # ── 视觉降级约束：detail/features 都为空时，禁止编造外观/颜色/材质 ──
    no_appearance_hint = ""
    if not has_detail and not has_features:
        no_appearance_hint = (
            f"\n【视觉信息缺失 · 禁止编造外观】\n"
            f"- 本次未提供产品外观、颜色、形状、材质等具体信息。\n"
            f"- 禁止描述产品的外形、颜色、尺寸、材质、配件、具体功能细节。\n"
            f"- 只能围绕「{topic}」写通用的情绪价值、场景代入、身份认同类内容，例如：\n"
            f"  · 提升气质 / 让你更自信 / 回头率翻倍\n"
            f"  · 解放双手 / 省心省力 / 节省时间\n"
            f"  · 找到属于自己的风格 / 满足你的小确幸\n"
            f"  · 治愈感 / 仪式感 / 氛围感拉满\n"
            f"- 即使引用模板中的占位逻辑，也必须改写成抽象的情绪/场景表达，绝不能凭空捏造具体外观描述。\n"
        )

    # ── 长度硬约束：把 target_chars_min/max 注入 prompt ──
    length_hint = ""
    if target_chars_min and target_chars_max:
        length_hint = (
            f"\n【字数硬性要求】必须严格控制在 {target_chars_min}-{target_chars_max} 个字符之间。"
            f"少于 {target_chars_min} 视频会短于目标时长，多于 {target_chars_max} 视频会超过目标时长。"
        )
    else:
        length_hint = "\n文案长度：5-8 句话，100-200 字。"

    return f"""{style_info['prompt']}

{template_block}

要求：
1. 口语化、有感染力、适合短视频平台（抖音/TikTok）
2. {length_hint}
3. **绝对不要使用占位符（不要【】、不要{{}}、不要"xx"、不要"XX"），所有句子必须写实
4. **绝对不要任何元描述/语气词标注** (不要在文案中输出"【惊讶式】""【超现实】""【超燃效果】""【BGM:】""【旁白:】"等任何带方括号的元描述)
5. **绝对不要 emoji 或装饰图标** (不要 🌟🌈✨🎉🔥💥 等任何 emoji 字符)
6. **绝对不要 markdown 格式** (不要 # 标题/不要 **加粗**/不要列表符号 - 1. -)
7. **直接输出文案正文, 前后不要任何额外说明** (不要"以下是文案:"/"文案:"等前缀, 不要"好的, 这是文案"等导语)
8. 必须围绕主题({topic})展开
{diversity_hint}{no_appearance_hint}
{lang_hint}
{extra_hint}

请直接输出文案："""


# ── DeepSeek API 模式 ──

def _call_deepseek_api(prompt: str, max_tokens: int = 512) -> Optional[str]:
    """调用 DeepSeek API 生成文案"""
    if not DEEPSEEK_API_KEY:
        return None

    try:
        import urllib.request
        import urllib.error

        data = json.dumps({
            "model": DEEPSEEK_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.7,
            "top_p": 0.9,
        }).encode("utf-8")

        req = urllib.request.Request(
            DEEPSEEK_API_URL,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            content = result["choices"][0]["message"]["content"]
            return content.strip()

    except Exception as e:
        print(f"[DeepSeek] API 调用失败: {e}")
        return None


# ── 本地 Qwen 模式 ──

_MODEL = None
_TOKENIZER = None
_MODEL_NAME = "Qwen/Qwen2.5-0.5B-Instruct"


def _load_local_model(progress_callback=None):
    """延迟加载 Qwen 模型

    Args:
        progress_callback: 可选进度回调函数
            下载中: (downloaded_bytes, total_bytes, "downloading")
            加载中: (0, 0, "loading")
            完成:   (1, 1, "done")
            失败:   (0, 0, "error")
    """
    global _MODEL, _TOKENIZER
    if _MODEL is not None:
        return True

    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        if progress_callback:
            progress_callback(0, 0, "检查模型缓存...")

        model_dir = None
        # 检查是否已缓存
        try:
            model_dir = Path.home() / ".cache" / "huggingface" / "hub" / "models--Qwen--Qwen2.5-0.5B-Instruct" / "snapshots"
            if model_dir.exists():
                snapshots = list(model_dir.iterdir())
                if snapshots:
                    model_dir = snapshots[0]
        except Exception:
            pass

        if model_dir and model_dir.exists():
            if progress_callback:
                progress_callback(1, 1, "模型已缓存，正在加载到内存...")
        else:
            if progress_callback:
                progress_callback(0, 1, "首次使用需要下载模型（约 1GB），请耐心等待...")

        print(f"[文案] 加载本地模型 {_MODEL_NAME}...")
        if progress_callback:
            progress_callback(0, 1, "正在加载分词器...")
        _TOKENIZER = AutoTokenizer.from_pretrained(_MODEL_NAME, trust_remote_code=True)

        if progress_callback:
            progress_callback(0, 1, "正在加载模型（首次下载约 1GB，需 10-60 秒）...")
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        _MODEL = AutoModelForCausalLM.from_pretrained(
            _MODEL_NAME,
            torch_dtype=torch.float16 if device == "mps" else torch.float32,
            device_map=device,
            trust_remote_code=True,
            low_cpu_mem_usage=True,
        )
        if progress_callback:
            progress_callback(1, 1, "模型加载完成！")
        print(f"[文案] 本地模型加载完成（设备: {device}）")
        return True
    except Exception as e:
        print(f"[文案] 本地模型加载失败: {e}")
        if progress_callback:
            progress_callback(0, 0, f"加载失败: {e}")
        return False


# 全局进度回调（由 UI 设置，供生成时显示加载进度）
_LOAD_PROGRESS_CALLBACK = None


def _call_local_model(prompt: str, max_length: int = 512,
                      progress_callback=None) -> Optional[str]:
    """调用本地 Qwen 模型生成"""
    global _LOAD_PROGRESS_CALLBACK
    if _MODEL is None:
        cb = progress_callback or _LOAD_PROGRESS_CALLBACK
        if not _load_local_model(progress_callback=cb):
            return None

    try:
        messages = [{"role": "user", "content": prompt}]
        text = _TOKENIZER.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        model_inputs = _TOKENIZER([text], return_tensors="pt").to(_MODEL.device)
        generated_ids = _MODEL.generate(
            **model_inputs,
            max_new_tokens=max_length,
            temperature=0.7,
            top_p=0.9,
            do_sample=True,
        )
        generated_ids = [
            output_ids[len(input_ids):]
            for input_ids, output_ids in zip(model_inputs.input_ids, generated_ids)
        ]
        return _TOKENIZER.batch_decode(generated_ids, skip_special_tokens=True)[0].strip()
    except Exception as e:
        print(f"[文案] 本地生成失败: {e}")
        return None


# ── 统一入口 ──

def _dedup_repetitions(text: str) -> str:
    """去除 AI 输出中的复读机式重复

    - 折叠连续重复短语: '限时特惠！限时特惠！限时优惠！' → '限时特惠！限时优惠！'
    - 同句出现 3+ 次仅保留 1 次
    - 优先按标点切分, 再去重整句
    """
    if not text:
        return text

    # 1. 按标点切分成句, 句内不允许重复 3+ 次
    # 切分: 中英文标点 (。！？!?;；)
    parts = re.split(r'([。！？!?;；\n]+)', text)
    # 重组为 [(content, sep), ...]
    sentences = []
    for i in range(0, len(parts) - 1, 2):
        sentences.append((parts[i], parts[i + 1]))
    if len(parts) % 2 == 1 and parts[-1].strip():
        sentences.append((parts[-1], ""))

    # 2. 整句去重: 统计相同句出现次数, 超过 1 次的全部移除
    seen_sentences = set()
    result = []
    for content, sep in sentences:
        s = content.strip()
        if not s:
            if sep:
                result.append(sep)
            continue
        # 句内重复短语检测: 如 "限时特惠！限时特惠！" 中 "限时特惠！" 重复了
        # 把句子按短语 (2-6 字) 滑窗统计, 如果同一短语连续出现 >=2 次, 删掉重复部分
        intra_dedup = _dedup_intra_phrase(s)
        if intra_dedup in seen_sentences:
            # 整句重复 → 跳过 (保留分隔符让句子不至于挤在一起)
            if sep:
                result.append(sep)
            continue
        seen_sentences.add(intra_dedup)
        result.append(intra_dedup)
        if sep:
            result.append(sep)

    text = "".join(result).rstrip()

    # 3. 二次清理: 句内连续重复短语 (类似 "限时特惠！限时特惠！") 折叠成单次
    text = _dedup_intra_phrase(text)
    # 4. 清理去重后残留的双标点: '！' / '??' / '。'
    text = re.sub(r'([。？！!?;；\n])\1+', r'\1', text)
    # 5. 去除每行开头存留 孤儿标点 (跳过重复分隔符营造出来)
    text = re.sub(r'(?m)^[\s。？！!?.,;；]+', '', text)

    return text


def _dedup_intra_phrase(s: str) -> str:
    """折叠句内连续重复的短语 (n-gram 去重)

    例:
      '限时特惠！限时特惠！限时优惠！' → '限时特惠！限时优惠！'
      '绝绝子！绝绝子！太爱了'        → '绝绝子！太爱了'
    """
    if not s:
        return s

    # 切分标点保留, 然后对 token 序列做连续 n-gram 去重
    # token = 连续非标点字符段
    tokens = re.split(r'([一-鿿]{2,8})', s)
    # 提取所有 2-6 字短语 (按 token 切分后的连续 token)
    # 简化做法: 直接对全句做 2-6 字滑窗, 看是否有连续重复
    out = s
    # 反复扫描直到稳定 (最多 3 轮)
    for _ in range(3):
        new_out = _collapse_runs(out)
        if new_out == out:
            break
        out = new_out
    return out


def _collapse_runs(s: str) -> str:
    """扫描字符串, 折叠连续重复的短语 (2-6 字)"""
    # 中文 2-6 字 + 可选标点
    pattern = re.compile(
        r'([一-鿿]{2,8})\1+'
    )
    while True:
        new_s = pattern.sub(r'\1', s)
        if new_s == s:
            break
        s = new_s
    return s


def _clean_result(text: str) -> str:
    """AI 文案后处理: 清洗模型常见的污染输出 (元描述/emoji/markdown/前缀)"""
    if not text:
        return ""
    # 0. 剥常见复合前缀 (干净源头, 如 "好的，以下是文案：")
    text = re.sub(r'^好的[,，:.。：]?\s*(?:以下是?\s*)?(?:文案\s*[:：]?\s*)?', '', text.strip(), count=1)
    # 1. 去除模型自加的前缀标签 ("文案:"/"好的, 以下是..."等)
    text = re.sub(r'^(文案|好的|当然|来|以下是|给你|请查收|这是一段)[：:]\s*', '', text.strip())
    # 2. 去除任何中文方括号【...】和英文方括号 [...] 元描述 (如 【惊讶式】【BGM:】【超现实】)
    text = re.sub(r'【[^】\n]*】', '', text)
    text = re.sub(r'\[[^]\n]*\]', '', text)
    # 3. 去除所有 emoji (覆盖 BMP/SMP/flags + 各种变体选择器 + ZWJ 序列)
    emoji_pattern = (
        "[\U0001F300-\U0001FAFF"
        "\U0001F600-\U0001F64F"
        "\U0001F680-\U0001F6FF"
        "\U0001F900-\U0001F9FF"
        "\U00002600-\U000027BF"
        "\U0001F100-\U0001F1FF"
        "\U0001F200-\U0001F2FF"
        "\u200D"
        "\uFE0F"
        "]"
    )
    text = re.sub(emoji_pattern, "", text)
    # 4. 去除 markdown 标记 (### 标题 / **加粗** / - 列表 / 1. 有序列表)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    text = re.sub(r'^\s*[-*+]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)
    # 5. 去除成对中文引号包裹
    text = re.sub(r'["""""]', '', text)
    # 6. 去除每行单独的中文冒号前缀残留 (如 "标题: xxx")
    text = re.sub(r'^[\u4e00-\u9fff]{1,10}[：:]\s*', '', text, flags=re.MULTILINE)
    # 7. 合并连续空行/前后空白
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]+\n', '\n', text)
    # 8. 复读机式重复去重 (放最后, 看到的是最干净的文本, 整句对比更准)
    #    例: "限时特惠！限时特惠！限时优惠！" -> "限时特惠！限时优惠！"
    text = _dedup_repetitions(text)
    return text.strip()


def _enforce_char_limit(text: str, min_chars=None, max_chars=None) -> str:
    """字数硬约束: 模型可能忽略 prompt 长度要求, 这里强制 trim 到 max_chars

    - max_chars: 截到该字符数之前最后一个句号/逗号/换行 (不破坏语义)
    - min_chars: 仅当结果短于 min_chars 时打 warning 日志 (不强制重生成)
    """
    if not text:
        return text
    if max_chars and len(text) > max_chars:
        head = text[:max_chars]
        for sep in ["。", "！", "?", "?", "!", "\n", "，", ","]:
            idx = head.rfind(sep)
            if idx > max_chars * 0.6:
                head = head[:idx + 1]
                break
        else:
            head = head.rstrip() + "。"
        text = head
        print(f"[文案] 超出 max_chars={max_chars}, 强制截断到 {len(text)} 字符")
    if min_chars and len(text) < min_chars:
        print(f"[文案] 警告: 结果 {len(text)} 字符 < min_chars={min_chars}, 偏短")
    return text


def _translate_if_needed(text: str, target_lang: str) -> str:
    """将文本翻译为目标语言，失败时返回原文"""
    if target_lang == "zh":
        return text
    try:
        return translate_text(text, target_lang=target_lang)
    except Exception as e:
        print(f"[文案] 翻译失败（将返回原文）: {e}")
        return text


def generate_script_by_topic(
    topic: str,
    style: str = "生活技巧",
    detail: Optional[str] = None,
    features: Optional[str] = None,
    lang: str = "zh",
    extra_instruction: Optional[str] = None,
    target_chars_min: Optional[int] = None,
    target_chars_max: Optional[int] = None,
) -> str:
    """根据选题生成口播文案

    自动选择后端：
    1. 如果设置了 DEEPSEEK_API_KEY 环境变量 → 调用 DeepSeek API
    2. 否则 → 使用本地 Qwen-0.5B 模型
    3. 都不可用 → 回退模板文案

    Args:
        topic: 选题/标题，如 "厨房收纳"
        style: 文案风格
        detail: 选题细节（可选）
        features: 特性描述（可选）
        lang: 语言 (zh/th/en)
        extra_instruction: 额外指令（可选）

    Returns:
        生成的文案
    """
    # 非中文时先生成中文，再翻译（Qwen/DeepSeek 对"请使用XX语输出"指令遵从不稳定）
    _target_lang = lang if lang and lang != "zh" else None

    # 从 extra_instruction 的"第N条"中解析 variation_index，让 batch 中每条角度不同
    _variation_index = 0
    if extra_instruction:
        _m_idx = re.search(r"第(\d+)条", extra_instruction)
        if _m_idx:
            _variation_index = int(_m_idx.group(1)) - 1  # 0-based

    prompt = _build_prompt(
        topic, style, detail, features,
        lang="zh",
        extra_instruction=extra_instruction,
        variation_index=_variation_index,
        target_chars_min=target_chars_min,
        target_chars_max=target_chars_max,
    )

    # 字数硬约束: max_tokens 按 target_chars_max * 2 给模型富余, 拿到结果后强制 trim
    _max_tokens = 512
    if target_chars_max:
        _max_tokens = max(512, min(2048, int(target_chars_max * 2)))

    # 模式 1: DeepSeek API (优先, 因为效果更好)
    if DEEPSEEK_API_KEY:
        result = _call_deepseek_api(prompt, max_tokens=_max_tokens)
        if result:
            result = _clean_result(result)
            result = _enforce_char_limit(result, target_chars_min, target_chars_max)
            if _target_lang:
                result = _translate_if_needed(result, _target_lang)
            return result

    # 模式 2: 本地 Qwen
    result = _call_local_model(prompt, max_length=_max_tokens)
    if result:
        result = _clean_result(result)
        result = _enforce_char_limit(result, target_chars_min, target_chars_max)
        if _target_lang:
            result = _translate_if_needed(result, _target_lang)
        return result

    # 模式 3：模板回退（带多语言翻译+多样性）
    style_info = STYLES.get(style, STYLES["生活技巧"])
    tpl = style_info["template"]
    fmt = {"topic": topic}
    if detail:
        fmt["topic_detail"] = detail
    else:
        # 移除模板中所有 {topic_detail} 引用，避免 "时尚女鞋最大的亮点就是时尚女鞋" 这种病句
        tpl = tpl.replace("{topic_detail}", topic)
    if features:
        fmt["topic_features"] = features
    else:
        tpl = tpl.replace("{topic_features}", topic)
    fallback = tpl.format(**fmt)
    # 根据 extra_instruction 制造差异（解析"第N条"）
    if extra_instruction:
        _m = re.search(r"第(\d+)条", extra_instruction)
        if _m:
            _idx = int(_m.group(1))
            _prefixes = ["家人们，", "朋友们，", "大家好，", "小伙伴们，", "亲爱的们，",
                          "各位注意啦，", "哈喽，", "宝宝们，", "集美们，", "宝子们，"]
            _prefix = _prefixes[_idx % len(_prefixes)]
            fallback = f"{_prefix}{fallback}（第{_idx}条）"
    # 非中文则翻译（统一使用 _translate_if_needed）
    if _target_lang:
        fallback = _translate_if_needed(fallback, _target_lang)
    return fallback


def list_styles() -> list[str]:
    """获取所有可用的文案风格"""
    return list(STYLES.keys())


def generate_publish_title(narration: str, lang: str = "zh",
                            max_chars: int = 20) -> str:
    """根据口播文案生成一句发布标题 (10~max_chars 字)

    自动选择后端（与 generate_script_by_topic 一致）：
    1. DEEPSEEK_API_KEY 设置 → DeepSeek API
    2. 否则 → 本地 Qwen-0.5B
    3. 全部失败 → 用 narration 首句前 max_chars 字 fallback

    Args:
        narration: 口播文案（会被截取前 300 字给模型，避免超长输入）
        lang: 输出语言 (zh/th/en), 非中文时会先生成中文再翻译
        max_chars: 标题最大字符数 (默认 20)

    Returns:
        单行发布标题字符串 (失败时返回 narration[:max_chars] 截断版)
    """
    if not narration or not narration.strip():
        return ""

    # 截取前 300 字给模型, 避免超长 narration 撑爆 prompt
    snippet = narration.strip()[:300]
    if len(narration.strip()) > 300:
        snippet += "…"

    _target_lang = lang if lang and lang != "zh" else None

    prompt = (
        "你是短视频平台发布标题生成助手。"
        "基于下方口播文案，生成一句发布标题。"
        f"硬性要求：1) 长度 {max(6, max_chars - 4)}~{max_chars} 个字符；"
        "2) 口语化、有钩子、能引发点击；"
        "3) 不要堆砌 emoji；"
        "4) 不要任何 markdown 标记（不要 # 不要 ** 不要列表符号）；"
        "5) 不要任何引号包裹；"
        "6) 只输出一行标题，不要任何解释或前缀。\n"
        f"口播文案：{snippet}"
    )

    # 模式 1: DeepSeek
    if DEEPSEEK_API_KEY:
        try:
            result = _call_deepseek_api(prompt, max_tokens=128)
            if result:
                result = _clean_result(result)
                result = _enforce_char_limit(result, max_chars=max_chars)
                if _target_lang:
                    result = _translate_if_needed(result, _target_lang)
                if result:
                    return result
        except Exception as _e:
            print(f"[标题] DeepSeek 生成失败, 走 Qwen/兜底: {_e}")

    # 模式 2: 本地 Qwen
    try:
        result = _call_local_model(prompt, max_length=128)
        if result:
            result = _clean_result(result)
            result = _enforce_char_limit(result, max_chars=max_chars)
            if _target_lang:
                result = _translate_if_needed(result, _target_lang)
            if result:
                return result
    except Exception as _e:
        print(f"[标题] Qwen 生成失败, 走兜底: {_e}")

    # 模式 3: 兜底 — 用 narration 第一句的前 max_chars 字
    import re as _re
    first_sent = _re.split(r"[。！？!?\n]", narration.strip(), maxsplit=1)[0].strip()
    if not first_sent:
        first_sent = narration.strip()
    fallback = first_sent[:max_chars]
    print(f"[标题] AI 全部失败, 用首句 fallback ({len(fallback)} 字): {fallback}")
    return fallback


def set_deepseek_key(api_key: str):
    """动态设置 DeepSeek API Key（运行时配置）"""
    global DEEPSEEK_API_KEY
    DEEPSEEK_API_KEY = api_key
    # 也可以通过写入 .env 文件持久化
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    env_path.write_text(f"DEEPSEEK_API_KEY={api_key}\n")
    print("[文案] DeepSeek API Key 已设置")


def check_deepseek_available() -> bool:
    """检查 DeepSeek API 是否已配置"""
    return bool(DEEPSEEK_API_KEY)


def generate_script_batch(topics: list[dict]) -> list[dict]:
    """批量生成文案

    Args:
        topics: [{"topic": str, "style": str, "detail": str, "features": str}, ...]

    Returns:
        [{"topic": str, "style": str, "script": str}, ...]
    """
    return [
        {
            "topic": item.get("topic", ""),
            "style": item.get("style", "生活技巧"),
            "script": generate_script_by_topic(
                topic=item.get("topic", ""),
                style=item.get("style", "生活技巧"),
                detail=item.get("detail"),
                features=item.get("features"),
            ),
        }
        for item in topics
    ]

def translate_text(text: str, target_lang: str = "th", source_lang: str = "zh-CN") -> str:
    """翻译文本（自动使用 DeepSeek 或 MyMemory 免费翻译）

    Args:
        text: 源文本
        target_lang: 目标语言代码
        source_lang: 源语言代码

    Returns:
        翻译后的文本

    Raises:
        RuntimeError: 所有翻译方式均失败
    """
    import subprocess
    import urllib.parse
    import urllib.request

    # 方案1: DeepSeek API（需要有效 Key）
    if DEEPSEEK_API_KEY and DEEPSEEK_API_KEY not in ("", "test_key_12345"):
        _lang_map = {
            "th": "泰文", "en": "英文", "zh": "中文", "zh-CN": "中文",
        }
        target_name = _lang_map.get(target_lang, target_lang)
        source_name = _lang_map.get(source_lang, source_lang)
        prompt = (
            f"请将以下{source_name}文本翻译成{target_name}。"
            f"只返回翻译结果，不要添加任何解释、注释或额外内容。\n\n"
            f"{text}"
        )
        try:
            payload = {
                "model": DEEPSEEK_MODEL,
                "messages": [
                    {"role": "system", "content": f"你是一个专业的翻译助手。将{source_name}翻译成{target_name}，只返回翻译结果。"},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": max(1024, len(text) * 3),
                "temperature": 0.3,
            }
            req = urllib.request.Request(
                DEEPSEEK_API_URL,
                data=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                },
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=30)
            result = json.loads(resp.read().decode())
            translated = result["choices"][0]["message"]["content"].strip()
            if translated:
                return translated
        except Exception as e:
            print(f"[翻译] DeepSeek 失败: {e}，使用 MyMemory 备份...")

    # 方案2: MyMemory 免费翻译（通过 curl 绕过 Python SSL 问题）
    # MyMemory 只支持简化的语言代码，zh-CN → zh, zh-TW → zh
    _mm_lang_map = {"zh-CN": "zh", "zh-TW": "zh", "zh-SG": "zh"}
    _mm_source = _mm_lang_map.get(source_lang, source_lang)
    _mm_target = _mm_lang_map.get(target_lang, target_lang)
    try:
        encoded_q = urllib.parse.quote(text, safe='')
        result = subprocess.run(
            ["curl", "-s", "-m", "5",
             f"https://api.mymemory.translated.net/get?q={encoded_q}&langpair={_mm_source}|{_mm_target}"],
            capture_output=True, text=True, timeout=8,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            translated = data.get("responseData", {}).get("translatedText", "")
            if translated and data.get("responseData", {}).get("match", 0) > 0.5:
                return translated
            print(f"[翻译] MyMemory 结果质量过低: {data.get('responseData', {}).get('match', 0)}")
        else:
            print(f"[翻译] MyMemory curl 失败: rc={result.returncode}")
    except Exception as e:
        print(f"[翻译] MyMemory 失败: {e}")

    raise RuntimeError(
        "所有翻译方式均失败\n"
        "请尝试: 1) 在 settings 中设置有效的 DeepSeek API Key\n"
        "         2) 或检查网络连接"
    )
