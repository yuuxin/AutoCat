"""AI 文案生成模块 — 双模式：本地 Qwen + DeepSeek API（选配）

功能：
1. 根据选题/关键词自动生成短视频口播文案
2. 支持多风格（种草、教程、测评、故事等）
3. 默认本地 Qwen-0.5B 离线运行
4. 用户明确选择 DeepSeek 时使用云端模式

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


def estimate_chars_for_duration_range(
    lang: str, duration_min: float, duration_max: float, rate_pct: int = 0,
    margin: float = 0.10,
) -> tuple[int, int]:
    """Return (chars_min, chars_max) for a duration range with optional margin.

    与 UI 显示路径使用同一套 margin (默认 0.10) — 这样 UI 提示的「预计文案
    字符范围」与后端 enforce 的 target_chars_min/target_chars_max 完全一致。
    之前的实现两边用不同 margin (UI=0.10, 后端=0) 导致 UI 107-156 / 后端
    119-142 这种用户困惑。

    margin 语义:
      chars_min = ideal_at_min_dur * (1 - margin) — 短时长方向可放宽下限
      chars_max = ideal_at_max_dur * (1 + margin) — 长时长方向可放宽上限
    """
    lo = min(duration_min, duration_max)
    hi = max(duration_min, duration_max)
    ideal_lo = estimate_chars_for_lang(lang, lo, rate_pct, margin=0)[2]
    ideal_hi = estimate_chars_for_lang(lang, hi, rate_pct, margin=0)[2]
    target_min = max(1, int(ideal_lo * (1 - margin)))
    target_max = int(ideal_hi * (1 + margin))
    return target_min, target_max

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
                  target_chars_max: Optional[int] = None,
                  video_type: Optional[str] = None) -> str:
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

    # v3.2: 【禁止捏造设计过程 / 过度承诺 / 跨品类】 — 始终启用, 与 validation 对应
    # 极简措辞以避免 prompt 过长 (test_core_unit 期望 < 1000 字符)
    no_fabrication_hint = (
        "\n【禁止捏造】以下文案会被系统拒收 (validate_script_quality 直接拦截)：\n"
        "- 设计过程 / 设计师故事：禁止「设计灵感」「设计故事」「设计理念」「设计师」「设计师的故事」「匠心」"
        "「匠心独运」「匠心打造」「手工打造」「精雕细琢」「精心打造」「精挑细选」「每一寸细节」等"
        "凭空捏造的设计过程或工艺细节。\n"
        "- 无支撑的过度承诺：禁止「艺术品」「完美展现」「极致」「独一无二」「绝佳」「殿堂级」「全新升级」"
        "「全球首发」「颠覆性」等无数据支撑的过度营销词。\n"
        "- 跨品类混淆：禁止把产品说成另一种东西。如选题是女鞋，不能说「一件衣服」「衣物」「外套」"
        "「裤子」「裙子」。保持品类一致。\n"
    )

    # ── 长度硬约束 + 输出格式硬约束 ──
    # v3.1: 把长度要求升级为"系统会强制 trim 超过 max 的部分，少于 min 拒收"
    #       + 显式禁止 hashtag、emoji、markdown、前缀导语（之前在要求列表里已有，
    #       但模型依旧会输出，这次提到【字数硬性要求】同一段强化权重）。
    length_hint = ""
    if target_chars_min and target_chars_max:
        length_hint = (
            f"\n【字数硬性要求】必须严格控制在 {target_chars_min}-{target_chars_max} 个字符之间。"
            f"少于 {target_chars_min} 视频会短于目标时长（系统拒收），"
            f"多于 {target_chars_max} 视频会超过目标时长（系统强制截断到 {target_chars_max} 字）。\n"
            f"**绝对禁止**输出：\n"
            f"  - 以'好的'/'文案:'/'以下是'/'当然'等导语开头的元描述\n"
            f"  - 任何形式的 # 标签（如 #经典单品 #品质保证）\n"
            f"  - emoji 字符（🌟✨🎉🔥💥 等）\n"
            f"  - markdown 标记（### 标题 / **加粗** / 列表符号 - 或 1.）\n"
            f"  - 中英文方括号元描述（【惊讶式】/[BGM]/[旁白] 等）\n"
            f"**直接以正文第一句开头，不要任何前缀。**"
        )
    else:
        length_hint = "\n文案长度：5-8 句话，100-200 字。"

    video_type_hint = ""
    if video_type:
        from autokat.core.ai_providers import video_type_prompt_hint
        video_type_hint = "\n" + video_type_prompt_hint(video_type) + "\n"

    return f"""{style_info['prompt']}

{video_type_hint}
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
{diversity_hint}{no_appearance_hint}{no_fabrication_hint}
{lang_hint}
{extra_hint}

请直接输出文案："""


# ── DeepSeek API 模式 ──

def _call_deepseek_api(prompt: str, max_tokens: int = 512,
                       *, api_key=None, api_url=None, model=None) -> Optional[str]:
    """调用 DeepSeek API 生成文案

    默认从模块级全局读取 DEEPSEEK_API_KEY / DEEPSEEK_API_URL / DEEPSEEK_MODEL；
    显式传入 ``api_key`` / ``api_url`` / ``model`` 时使用调用方提供的值，
    DeepSeekWriterProvider 通过这种方式注入 ai_settings 中的 URL 和模型名。
    """
    effective_key = (api_key if api_key is not None else DEEPSEEK_API_KEY) or ""
    effective_url = api_url or DEEPSEEK_API_URL
    effective_model = model or DEEPSEEK_MODEL
    if not effective_key:
        return None

    try:
        import urllib.request
        import urllib.error

        data = json.dumps({
            "model": effective_model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.7,
            "top_p": 0.9,
        }).encode("utf-8")

        req = urllib.request.Request(
            effective_url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {effective_key}",
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            content = result["choices"][0]["message"]["content"]
            return content.strip()

    except urllib.error.HTTPError as e:
        # v3.2: 针对 401/403/429 等常见 HTTP 错误给出可操作的提示,
        # 而不是只看 print 日志猜问题。
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:400]
        except Exception:
            err_body = ""
        if e.code == 401:
            msg = (f"API Key 认证失败 (401)。请到 platform.deepseek.com 检查 Key "
                   f"是否已过期/被禁用，然后到 AI 对话框重新填入。")
        elif e.code == 402:
            msg = "账户余额不足 (402)。请到 platform.deepseek.com 充值。"
        elif e.code == 429:
            msg = "请求频率超限 (429)。请等几分钟后重试或减小并发。"
        else:
            msg = f"HTTP {e.code} 错误。响应: {err_body}"
        print(f"[DeepSeek] {msg}")
        # 改用 raise 让上游 DeepSeekWriterProvider.generate 接住, 在 UI 日志里
        # 显示完整路径 (Keychain 路径 / 文件路径 / 错误原因)
        raise RuntimeError(f"DeepSeek API {msg}") from e
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


# --- v2.4: 元描述前缀剥除 (模块级预编译, 单条覆盖度高的正则 + chr() 构造中文括号) ---
# 覆盖 '当然可以!以下是...' / '以下是...' / '好的, 以下是...' / '文案:' 等。
# 关键改动 (相对 v2.4 上一版):
#   1) 关键 keywords 用 + 替代 ? (允许 '短视频带货文案' 这种多关键词组合)
#   2) 收尾的冒号 [：:]? 设为可选 (用户报 '以下是围绕XX的短视频带货文案' 没冒号也能剥)
#   3) 中文括号「」/《》用 chr() 拼, 避免 raw string 中文括号在某些编辑器/Python 版本下解析问题
_LDQ = chr(0x300C)  # 「
_RDQ = chr(0x300D)  # 」
_LDB = chr(0x300A)  # 《
_RDB = chr(0x300B)  # 》
_SEP = r'[\s,，。!！?？~～]*'
_TOPIC_RE = (
    r'(?:'
    + _LDQ + r'[^' + _RDQ + r'\n]*' + _RDQ   # 「...」
    + r'|"[^"\n]*"'                            # "..."
    + r'|' + _LDB + r'[^' + _RDB + r'\n]*' + _RDB   # 《...》
    + r'|[^' + _LDQ + chr(34) + _LDB + r'\s,，。!！?？~～\n]{1,40}'   # 普通 1-40 字
    + r')?'
)

_META_PREFIX_PATTERNS = [
    re.compile(
        r'^'
        + r'(?:当然可以?|当然|可以|好的|没问题|收到|OK)?' + _SEP
        + r'(?:以下是?)?' + _SEP
        + r'(?:一段|一个|一些|份)?' + _SEP
        + r'(?:关于|围绕|有关|针对|描述)?' + _SEP
        + _TOPIC_RE + _SEP
        + r'(?:的)?' + _SEP
        + r'(?:短视频|带货|口播|种草|文案|广告|内容|话术|开场|标题)+' + _SEP
        + r'[：:]?' + _SEP
    ),
]

# v2.4 补充: 纯元描述兜底 — 整段文本从开头到结尾都没有真实产品内容的元回复
# (如 '好的, 以下是文案:' / '以下是文案' 这种只回元描述没产品内容的整段污染)
_PURE_META_RE = re.compile(
    r'^' + _SEP
    + r'(?:当然可以?|当然|可以|好的|没问题|收到|OK)' + _SEP
    + r'(?:以下是?)?' + _SEP
    + r'(?:一段|一个|一些|份)?' + _SEP
    + r'(?:关于|围绕|有关|针对|描述)?' + _SEP
    + _TOPIC_RE + _SEP
    + r'(?:的)?' + _SEP
    + r'(?:短视频|带货|口播|种草|文案|广告|内容|话术|开场|标题)*' + _SEP
    + r'[：:]?' + _SEP
    + r'$'
)




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


def _clean_result(text: str, topic: str = None) -> str:
    """AI 文案后处理: 清洗模型常见的污染输出 (元描述/emoji/markdown/前缀)"""
    if not text:
        return ""
    text = text.strip()
    # 0. 剥"先感叹/确认 → 再元描述"复合前缀 (如 "当然可以！以下是一段围绕XX的短视频带货文案：实际内容")
    #    v2.4: 用 re.match + 切片替代 re.sub, 绕开 Python regex 在多步回溯时静默不匹配的坑
    # v2.4: 用多条小正则逐个试, 绕开 Python regex 多步嵌套回溯时静默不匹配的坑
    for _meta_pat in _META_PREFIX_PATTERNS:
        _m = _meta_pat.match(text)
        if _m:
            text = text[_m.end():]
            break
    # 1. 剥单行元描述前缀 (如 "文案:"/"以下是:" 等)
    text = re.sub(r'^(文案|好的|当然|来|以下是|给你|请查收|这是一段)[：:]\s*', '', text)
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
    # 4b. 去除行内 "# xxx" hashtag 标签 (如 "#经典单品 #品质保证")
    #     行首或行内的 hashtag 都剥，避免在成片里出现 #标签
    text = re.sub(r'#\S+', '', text)
    # 4c. 修复孤字/残字: 当 1-2 个汉字被标点/空格孤立夹在中间时 (如 "节女鞋")，
    #     极有可能是 tokenizer 截断。删掉这个孤字让前后语义连续，或替换为 topic 前两字。
    #     规则: 前后都是空格或标点，自身是 1-2 个汉字，单独成"词"。
    # 用 topic 的前 2 个汉字作为兜底替换词；无 topic 则删掉孤字让前后连贯
    _topic_fill = ""
    if topic:
        _topic_fill = re.sub(r'\s+', '', topic)[:2] or topic[:1]
    text = re.sub(
        r'(?<=[\s，。！？!?、；;,.])([一-鿿]{1,2})(?=[\s，。！？!?、；;,.]|$)',
        lambda _m: _topic_fill if (_topic_fill and _m.group(1) != _topic_fill) else '',
        text,
    )
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
    # 9. v2.4: 纯元描述兜底 — 剥了前缀后整段如果仍是纯元描述 (整段匹配 _PURE_META_RE),
    #    直接返回空串让上层 fallback. 之前用 "< 6 中文字 + 无内容动词" 太激进,
    #    把 "今天" / "实际内容" 这种短但真实的内容也删了.
    _stripped = text.strip()
    if _stripped and _PURE_META_RE.match(_stripped):
        return ""
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


_META_REPLY_PATTERNS = (
    "请告诉我", "请提供", "需要更多信息", "需要您提供", "想要的主题",
    "我可以为您", "我能为您", "这样我就能", "作为ai", "作为 AI",
    "无法生成", "不能生成", "抱歉",
)
_UNSUPPORTED_PRODUCT_CLAIMS = (
    "面料", "材质", "环保", "透气", "透湿", "防水", "真皮", "皮革",
    "棉质", "颜色", "黑色", "白色", "红色", "高跟鞋", "平底鞋",
    "增高", "防滑", "耐磨", "矫正", "按摩", "抗菌",
)
_PROMOTION_WORDS = ("限时", "抢购", "特价", "特惠", "优惠", "秒杀", "直降", "折扣")

# v3.2: 凭空捏造的设计过程/设计师故事 — 未提供素材细节时不能编造
_FABRICATED_PROCESS_CLAIMS = (
    "设计灵感", "设计故事", "设计理念", "设计哲学",
    "匠心", "匠心独运", "匠心打造", "匠心呈现",
    "设计师", "设计师的故事", "设计师的灵感",
    "手工打造", "手工制作", "纯手工",
    "精雕细琢", "精心打造", "精心设计", "精挑细选",
    "每一寸细节", "每一道工序", "每一处细节",
    "反复打磨", "千锤百炼",
)

# v3.2: 无具体数据/材质支撑的过度承诺词
_OVERCLAIMS_NO_SUPPORT = (
    "艺术品", "完美", "完美展现", "完美呈现", "完美融合",
    "极致", "独一无二", "绝佳", "殿堂级", "顶配",
    "全新升级", "全球首发", "颠覆性", "革命性",
)

# v3.2: 跨品类混淆 — topic 类别 → text 里不应出现的另一类核心词
# 启发式: 当 topic 是 X 类时, text 把 X 描述成 Y 类核心词就标记为跨品类混淆
_CROSS_CATEGORY_FORBIDDEN = {
    # topic 含鞋相关词 → text 不能出现衣服/裤/裙等品类词 (除非作为搭配对象且未混淆主语)
    ("鞋",): ("衣服", "衣物", "上衣", "外套", "衬衫", "T恤", "卫衣",
              "裤子", "裙", "连衣裙"),
    # topic 含衣服/裤/裙等 → text 不能出现鞋/靴/袜品类词
    ("衣", "裤", "裙", "衫"): ("鞋子", "运动鞋", "皮鞋", "高跟鞋", "靴子", "袜子"),
    # topic 含包 → text 不能出现衣服/鞋
    ("包",): ("衣服", "衣物", "鞋子"),
    # topic 含帽 → text 不能出现衣服/鞋
    ("帽",): ("衣服", "衣物", "鞋子"),
}

_TOPIC_CATEGORY_KEYS = (
    ("女鞋", "鞋子", "男鞋", "童鞋", "运动鞋", "皮鞋", "高跟鞋", "平底鞋",
     "靴子", "凉鞋", "拖鞋", "袜子", "鞋"),
    ("衣服", "上衣", "外套", "衬衫", "T恤", "卫衣", "风衣", "羽绒服",
     "裤子", "裙", "连衣裙"),
    ("包", "包包", "背包", "手提包", "钱包"),
    ("帽", "帽子", "鸭舌帽", "渔夫帽"),
)


def _detect_topic_category(topic: str) -> Optional[str]:
    """根据 topic 关键词粗略判定品类 (鞋 / 衣 / 包 / 帽 / None)。

    用于跨品类混淆检测。如果 topic 是混合品类 (如 "穿搭" 这种风格词)，
    返回 None 跳过检测。
    """
    if not topic:
        return None
    for idx, keywords in enumerate(_TOPIC_CATEGORY_KEYS):
        for kw in keywords:
            if kw in topic:
                return ("鞋", "衣", "包", "帽")[idx]
    return None


def _content_char_count(text: str) -> int:
    return len(re.sub(r"\s+", "", text or ""))


def _is_wildly_off(text: str, min_chars: int, max_chars: int) -> bool:
    """检查模型输出是否严重偏离目标字数 (说明模型没理解长度约束)。

    阈值:
      - 输出 > 1.5x max_chars: 太长，模型没在控制
      - 输出 < 0.5x min_chars: 太短，模型没在控制
    返回 True 时调用方应跳过剩余 retry 直接走 fallback。
    """
    if not text:
        return True
    actual = _content_char_count(text)
    return actual > max_chars * 1.5 or actual < min_chars * 0.5


def _topic_terms(topic: str) -> list[str]:
    compact = re.sub(r"\s+", "", topic or "")
    terms = [compact] if compact else []
    terms.extend(
        part for part in re.findall(r"[\u4e00-\u9fff]{2,}", compact)
        if part not in terms
    )
    if len(compact) >= 4:
        terms.extend(compact[index:index + 2] for index in range(len(compact) - 1))
    return list(dict.fromkeys(term for term in terms if len(term) >= 2))


def script_similarity(left: str, right: str, ngram: int = 3) -> float:
    """Character n-gram Jaccard similarity for batch quality gating."""
    def grams(text: str) -> set[str]:
        compact = re.sub(r"[\s，。！？!?、；;：:,.]+", "", text or "")
        if len(compact) < ngram:
            return {compact} if compact else set()
        return {compact[index:index + ngram] for index in range(len(compact) - ngram + 1)}
    a, b = grams(left), grams(right)
    return len(a & b) / len(a | b) if a or b else 0.0


def validate_script_quality(
    text: str,
    topic: str,
    lang: str = "zh",
    target_chars_min: Optional[int] = None,
    target_chars_max: Optional[int] = None,
    detail: Optional[str] = None,
    features: Optional[str] = None,
    accepted_texts: Optional[list[str]] = None,
    similarity_threshold: float = 0.70,
    require_topic: bool = True,
) -> dict:
    """Validate one generated script before it is exposed to the UI."""
    text = (text or "").strip()
    reasons = []
    char_count = _content_char_count(text)
    lower = text.lower()
    if not text:
        reasons.append("空文案")
    if any(pattern.lower() in lower for pattern in _META_REPLY_PATTERNS):
        reasons.append("检测到追问、拒答或元回复")
    if require_topic and not any(term in text for term in _topic_terms(topic)):
        reasons.append("正文未围绕选题")
    if target_chars_min and char_count < target_chars_min:
        reasons.append(f"字数不足: {char_count} < {target_chars_min}")
    if target_chars_max and char_count > target_chars_max:
        reasons.append(f"字数超限: {char_count} > {target_chars_max}")
    if re.search(r"[{【\[][^}\]】]*[}】\]]|(?:xx|XX)", text):
        reasons.append("包含占位符或元描述")
    if re.search(r"(?:—{2,}|-{2,})\s*[！!。.]|[——-]{2,}\s*$", text):
        reasons.append("包含残缺标题或句子")
    promo_count = sum(text.count(word) for word in _PROMOTION_WORDS)
    if promo_count > 2:
        reasons.append(f"促销词堆叠: {promo_count} 次")
    if not detail and not features:
        claims = [
            claim for claim in _UNSUPPORTED_PRODUCT_CLAIMS
            if claim in text and claim not in topic
        ]
        if claims:
            reasons.append("包含未提供的具体属性: " + "、".join(claims[:5]))
    # v3.2: 凭空捏造的设计过程 / 设计师故事 — 不论有没有提供素材，都不允许出现
    #        因为本地模型会编造不存在的设计师/灵感/工艺细节来填充篇幅
    process_claims = [
        claim for claim in _FABRICATED_PROCESS_CLAIMS if claim in text
    ]
    if process_claims:
        reasons.append("凭空捏造设计过程/设计师故事: " + "、".join(process_claims[:5]))
    # v3.2: 无数据支撑的过度承诺词 — 模型为了凑字数经常叠加这种词
    overclaims = [claim for claim in _OVERCLAIMS_NO_SUPPORT if claim in text]
    if overclaims:
        reasons.append("无支撑的过度承诺: " + "、".join(overclaims[:5]))
    # v3.2: 跨品类混淆 — topic 是鞋/衣/包/帽, text 里却把产品描述为另一品类
    category = _detect_topic_category(topic)
    if category:
        forbidden = []
        for cat_keys, bad_words in _CROSS_CATEGORY_FORBIDDEN.items():
            if category in cat_keys:
                forbidden.extend(bad_words)
        cross_hits = [w for w in forbidden if w in text]
        if cross_hits:
            reasons.append(f"跨品类混淆: 选题是「{category}」但文案出现了 {cross_hits[:3]}")
    if lang == "zh":
        zh_count = sum("\u4e00" <= char <= "\u9fff" for char in text)
        foreign_count = sum(char.isalpha() and not ("\u4e00" <= char <= "\u9fff") for char in text)
        if zh_count == 0 or foreign_count > max(20, zh_count):
            reasons.append("输出语言不是中文")
    elif lang == "en":
        if sum(char.isascii() and char.isalpha() for char in text) < char_count * 0.45:
            reasons.append("输出语言不是英文")
    elif lang == "th":
        if sum("\u0e00" <= char <= "\u0e7f" for char in text) < char_count * 0.35:
            reasons.append("输出语言不是泰文")
    similarities = [script_similarity(text, previous) for previous in (accepted_texts or [])]
    max_similarity = max(similarities, default=0.0)
    if max_similarity > similarity_threshold:
        reasons.append(f"与同批文案过于相似: {max_similarity:.2f} > {similarity_threshold:.2f}")
    return {
        "valid": not reasons,
        "reasons": reasons,
        "char_count": char_count,
        "max_similarity": round(max_similarity, 4),
    }


_SAFE_OPENERS = [
    "想让日常穿搭更有记忆点，可以先从一双时尚女鞋开始。",
    "衣柜里的搭配总觉得少点感觉？时尚女鞋往往就是点亮造型的关键。",
    "通勤、逛街或朋友聚会，一双时尚女鞋都能让整体状态更利落。",
    "真正省心的穿搭，不是堆很多单品，而是选对一双时尚女鞋。",
    "今天不聊复杂搭配，只聊时尚女鞋怎样帮你轻松切换不同场景。",
    "如果你也在寻找日常造型的新灵感，不妨把目光放到时尚女鞋上。",
    "穿搭想显得更完整，时尚女鞋是一个简单又直接的切入点。",
    "从早上的通勤到晚上的聚会，时尚女鞋能陪你找到自己的节奏。",
]
_SAFE_BODIES = [
    "它适合放进日常搭配思路里，让普通造型多一点个人表达。出门前不用反复纠结，也能更快找到舒服自在的状态。",
    "你可以根据当天的心情调整整体风格，让每次出门都有一点新鲜感。重点不是追赶潮流，而是穿出属于自己的自信。",
    "它能自然融入不同生活场景，让你的步伐和气质都更从容。简单搭配也可以有层次，日常记录更容易留下亮点。",
    "选鞋时可以先想想自己的生活节奏，再决定今天想表达怎样的感觉。适合自己的选择，往往更容易带来长期的愉悦。",
    "无论今天安排紧凑还是轻松随性，都可以用它完成造型上的呼应。穿搭不必复杂，清晰的个人风格就很有吸引力。",
    "它给日常造型增加了更多组合空间，也让你在不同场合保持自然。每一次出门，都是重新表达自己的机会。",
    "把注意力放回自己的感受，穿搭就会变得更轻松。它不需要喧宾夺主，也能帮助整体造型找到合适的重点。",
    "当你想改变状态时，可以先从脚下的选择开始。小小的搭配变化，也能让普通一天多一点仪式感。",
]
_SAFE_ENDINGS = [
    "如果你喜欢轻松又有个人感的穿搭，时尚女鞋值得加入你的日常选择。",
    "找到适合自己的搭配节奏，让时尚女鞋陪你自信走进每一个场景。",
    "不必复制别人的答案，用时尚女鞋穿出你自己的日常风格。",
    "把喜欢的感觉穿在身上，让时尚女鞋成为你表达自己的方式。",
]


def build_safe_script(topic: str, variation_index: int = 0,
                      target_chars_min: Optional[int] = None,
                      target_chars_max: Optional[int] = None) -> str:
    """Build a deterministic, claim-free script that still passes quality gates."""
    topic = topic.strip()
    opener = _SAFE_OPENERS[variation_index % len(_SAFE_OPENERS)].replace("时尚女鞋", topic)
    body = _SAFE_BODIES[variation_index % len(_SAFE_BODIES)]
    ending = _SAFE_ENDINGS[variation_index % len(_SAFE_ENDINGS)].replace("时尚女鞋", topic)
    text = opener + body + ending
    fillers = [
        "真正适合日常的穿搭灵感，会让你更愿意记录生活里的每一步。",
        "当整体造型和自己的状态互相呼应，自信也会自然流露出来。",
        "不用刻意迎合固定答案，舒服地表达自己就是很好的风格。",
    ]
    index = 0
    while target_chars_min and _content_char_count(text) < target_chars_min:
        text += fillers[(variation_index + index) % len(fillers)]
        index += 1
    return _enforce_char_limit(text, target_chars_min, target_chars_max)


def _translate_if_needed(text: str, target_lang: str, provider: str = "local") -> str:
    """Translate through the explicitly selected provider."""
    if target_lang == "zh":
        return text
    return translate_text(text, target_lang=target_lang, provider=provider)


def generate_script_by_topic_detailed(
    topic: str,
    style: str = "生活技巧",
    detail: Optional[str] = None,
    features: Optional[str] = None,
    lang: str = "zh",
    extra_instruction: Optional[str] = None,
    target_chars_min: Optional[int] = None,
    target_chars_max: Optional[int] = None,
    accepted_texts: Optional[list[str]] = None,
    progress_callback=None,
    max_attempts: int = 3,
    provider: str = "local",
    material_capabilities: Optional[str] = None,
    video_type: Optional[str] = None,
) -> dict:
    """Generate one quality-gated script and return text plus diagnostics."""
    # 非中文时先生成中文，再翻译（Qwen/DeepSeek 对"请使用XX语输出"指令遵从不稳定）
    _target_lang = lang if lang and lang != "zh" else None

    # 从 extra_instruction 的"第N条"中解析 variation_index，让 batch 中每条角度不同
    _variation_index = 0
    if extra_instruction:
        _m_idx = re.search(r"第(\d+)条", extra_instruction)
        if _m_idx:
            _variation_index = int(_m_idx.group(1)) - 1  # 0-based

    _max_tokens = 512
    if target_chars_max:
        _max_tokens = max(512, min(2048, int(target_chars_max * 2)))

    # Provider selection goes through ai_providers.build_writer_provider so
    # the macOS Keychain and ai_settings.json are the single source of truth.
    # A failed provider never silently switches to a different provider.
    from autokat.core.ai_providers import build_writer_provider
    provider_obj = build_writer_provider(provider)
    backend_name = type(provider_obj).__name__
    backends = [(backend_name, provider_obj.generate)]

    last_reasons = []

    for backend_name, call_backend in backends:
        for attempt in range(1, max(1, max_attempts) + 1):
            retry_hint = ""
            if last_reasons:
                retry_hint = (
                    "\n上一版未通过质量校验，必须修正以下问题："
                    + "；".join(last_reasons)
                )
            prompt = _build_prompt(
                topic, style, detail, features, lang="zh",
                extra_instruction=(extra_instruction or "") + retry_hint,
                variation_index=_variation_index + attempt - 1,
                target_chars_min=target_chars_min,
                target_chars_max=target_chars_max,
                video_type=video_type,
            )
            if material_capabilities:
                prompt += (
                    "\n已选素材能力摘要：" + material_capabilities
                    + "\n文案只能围绕这些可展示能力组织表达，不得编造素材无法支持的画面。"
                )
            if progress_callback:
                progress_callback(
                    backend_name, attempt, max_attempts,
                    "正在生成" if not last_reasons else "；".join(last_reasons),
                )
            try:
                raw = provider_obj.generate(prompt, max_tokens=_max_tokens)
            except Exception as _call_err:
                raw = None
                print(f"[文案质量] {backend_name} 调用失败: {_call_err}")
            result = _clean_result(raw, topic=topic) if raw else ""
            # 强制 trim 到字数范围 (本地模型经常无视 prompt 长度要求)
            if result and (target_chars_min or target_chars_max):
                result = _enforce_char_limit(
                    result,
                    min_chars=target_chars_min,
                    max_chars=target_chars_max,
                )
            # 早 fail: 首次输出严重偏离目标范围 (>1.5x 或 <0.5x)，模型明显
            # 没理解长度约束——不再浪费后续 retry，直接 break 让 fallback 兜底
            if attempt == 1 and target_chars_min and target_chars_max and result:
                if _is_wildly_off(result, target_chars_min, target_chars_max):
                    print(
                        f"[文案质量] {backend_name} 首次输出严重偏离目标范围，"
                        f"跳过剩余重试，fallback 兜底"
                    )
                    break
            if _target_lang:
                source_quality = validate_script_quality(
                    result, topic, lang="zh", detail=detail, features=features,
                )
                if source_quality["valid"]:
                    result = _translate_if_needed(result, _target_lang, provider=provider)
                    quality = validate_script_quality(
                        result, topic, lang=lang,
                        target_chars_min=target_chars_min,
                        target_chars_max=target_chars_max,
                        detail=detail, features=features,
                        accepted_texts=accepted_texts, require_topic=False,
                    )
                else:
                    quality = source_quality
            else:
                quality = validate_script_quality(
                    result, topic, lang=lang,
                    target_chars_min=target_chars_min,
                    target_chars_max=target_chars_max,
                    detail=detail, features=features,
                    accepted_texts=accepted_texts,
                )
            if quality["valid"]:
                return {"text": result, "source": backend_name, "quality": quality}
            last_reasons = quality["reasons"] or ["模型未返回有效正文"]
            print(
                f"[文案质量] {backend_name} 第 {attempt}/{max_attempts} 次不合格: "
                + "；".join(last_reasons)
            )

    # v3.2: 移除 build_safe_script 兜底 (用户报告: 兜底模板「无意义」)。
    # AI 重试耗尽后, 直接 raise 清晰错误, 异常路径直达 UI 日志, 用户看到后
    # 知道是哪个 provider 失败 / 为什么失败, 然后在文本框手动录入。
    _reasons = sorted(set(last_reasons))[:3] or ["模型未返回有效正文"]
    raise RuntimeError(
        f"{backend_name} 生成文案失败 (重试 {max_attempts} 次均不合格): "
        + "；".join(_reasons)
        + f"\n请在 AI 辅助生成对话框下方的文本框中手动录入文案, "
        f"或检查 {backend_name} 配置 (Key/网络/模型名) 后重试。"
    )


def generate_script_by_topic(
    topic: str,
    style: str = "生活技巧",
    detail: Optional[str] = None,
    features: Optional[str] = None,
    lang: str = "zh",
    extra_instruction: Optional[str] = None,
    target_chars_min: Optional[int] = None,
    target_chars_max: Optional[int] = None,
    provider: str = "local",
    material_capabilities: Optional[str] = None,
) -> str:
    """Compatibility entrypoint returning only the quality-gated script text."""
    return generate_script_by_topic_detailed(
        topic, style, detail, features, lang=lang,
        extra_instruction=extra_instruction,
        target_chars_min=target_chars_min,
        target_chars_max=target_chars_max,
        provider=provider,
        material_capabilities=material_capabilities,
    )["text"]


def list_styles() -> list[str]:
    """获取所有可用的文案风格"""
    return list(STYLES.keys())


# v3.2: UI 显示用的文案风格标签 — key 不变, prompt 内部描述保留
# 命名原则: 用「身份+腔调」让用户一眼看出博主人设
STYLE_LABELS: dict[str, str] = {
    "种草推荐": "带货博主",
    "生活技巧": "生活达人",
    "知识科普": "科普老师",
    "测评对比": "实测派",
    "励志感悟": "走心姐姐",
}
# v3.2: UI 上加 "?" 图标的 tooltip 文案
STYLE_TOOLTIP = (
    "决定 AI 用什么腔调讲话（博主人设）。\n"
    "默认按视频类型自动匹配，展开「⚙ 高级」后可手动覆盖。"
)


def list_style_choices() -> list[tuple[str, str]]:
    """返回 [(display_label, key), ...] 顺序, 用于 UI 下拉框构建。"""
    return [(STYLE_LABELS.get(k, k), k) for k in STYLES.keys()]


def generate_publish_title(narration: str, lang: str = "zh",
                           max_chars: int = 20, provider: str = "local") -> str:
    """根据口播文案生成一句发布标题 (10~max_chars 字)

    Only the explicitly selected provider is called. Failure uses a
    deterministic first-sentence fallback and never switches provider.

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

    if provider not in ("local", "deepseek"):
        raise ValueError(f"不支持的文案模型: {provider}")
    if provider == "deepseek" and not DEEPSEEK_API_KEY:
        raise RuntimeError("已选择 DeepSeek，但尚未配置有效 API Key")
    # v3.2: 移除「用 narration 首句截断」兜底 (用户报告: 「无意义」)。
    # 标题 AI 失败时直接 raise, UI / 上游看到后让用户手动录入标题。
    # 必须真的调一次 provider — 失败信息里要告诉用户「deepseek 调失败」
    # 或「local 加载失败」, 而不是凭空说「调用失败」。
    _errs = []
    if provider == "deepseek":
        try:
            result = _call_deepseek_api(prompt, max_tokens=128)
            if result:
                result = _clean_result(result)
                result = _enforce_char_limit(result, max_chars=max_chars)
                if _target_lang:
                    result = _translate_if_needed(result, _target_lang, provider=provider)
                if result:
                    return result
        except Exception as _e:
            print(f"[标题] DeepSeek 生成失败: {_e}")
        _errs.append("DeepSeek 调用失败 (检查 API Key/网络/余额)")
    if provider == "local":
        try:
            result = _call_local_model(prompt, max_length=128)
            if result:
                result = _clean_result(result)
                result = _enforce_char_limit(result, max_chars=max_chars)
                if _target_lang:
                    result = _translate_if_needed(result, _target_lang, provider=provider)
                if result:
                    return result
        except Exception as _e:
            print(f"[标题] Qwen 生成失败: {_e}")
        _errs.append("本地模型加载失败或无有效输出")
    raise RuntimeError(
        f"AI 标题生成失败 ({'; '.join(_errs) or '未知'})。"
        f"请在任务列表/发布页的标题栏手动录入, "
        f"或检查 {provider} 配置后重试。"
    )


def set_deepseek_key(api_key: str):
    """Set the legacy in-process key without writing secrets to disk."""
    global DEEPSEEK_API_KEY
    DEEPSEEK_API_KEY = api_key
    print("[文案] DeepSeek API Key 已设置")


def set_deepseek_config(api_key: str, api_url: Optional[str] = None,
                        model: Optional[str] = None, persist_legacy_env: bool = False):
    """Configure DeepSeek explicitly; new UI stores the key in macOS Keychain."""
    global DEEPSEEK_API_KEY, DEEPSEEK_API_URL, DEEPSEEK_MODEL
    DEEPSEEK_API_KEY = api_key.strip()
    if api_url:
        DEEPSEEK_API_URL = api_url.strip()
    if model:
        DEEPSEEK_MODEL = model.strip()
    if persist_legacy_env:
        set_deepseek_key(DEEPSEEK_API_KEY)


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
                provider=item.get("provider", "local"),
            ),
        }
        for item in topics
    ]

def translate_text(text: str, target_lang: str = "th", source_lang: str = "zh-CN",
                   provider: str = "local") -> str:
    """Translate using only the explicitly selected writer provider.

    配置从 ai_providers 单一来源读取（DeepSeek 时 Keychain + ai_settings.json），
    选定 Provider 失败后不会静默切换到其他 Provider。
    """
    if provider not in ("local", "deepseek"):
        raise ValueError(f"不支持的文案模型: {provider}")
    _lang_map = {"th": "泰文", "en": "英文", "zh": "中文", "zh-CN": "中文"}
    target_name = _lang_map.get(target_lang, target_lang)
    source_name = _lang_map.get(source_lang, source_lang)
    prompt = (
        f"请将以下{source_name}文本翻译成{target_name}。"
        f"只返回翻译结果，不要添加任何解释、注释或额外内容。\n\n{text}"
    )
    from autokat.core.ai_providers import build_writer_provider
    provider_obj = build_writer_provider(provider)
    try:
        raw = provider_obj.generate(prompt, max_tokens=max(1024, len(text) * 3))
    except Exception as e:
        if provider == "deepseek":
            raise RuntimeError(f"DeepSeek 翻译失败: {e}") from e
        raise RuntimeError(f"本地模型翻译失败: {e}") from e
    translated = _clean_result(raw) if raw else ""
    if translated:
        return translated
    if provider == "deepseek":
        raise RuntimeError("DeepSeek 未返回有效翻译")
    raise RuntimeError(f"{provider} 未返回有效翻译")
