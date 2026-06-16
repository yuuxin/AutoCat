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


def _format_capability_summary_prompt(capability_summary: str) -> str:
    """v3.5 (方案 A): 把切片分析出的素材能力摘要拼到 prompt 里。

    - 旧版用负面措辞 ("不得编造素材无法支持的画面") 堵死了小模型,
      让 Qwen 0.5B 走投无路自己编产品名 (如 "无界运动鞋")。
    - 新版改成正向引导 ("可以用这些能力描述具体场景"),
      给可操作的例子 (特写/通勤/户外自然光),
      让小模型照搬 summary 里的 (主体/景别/场景/动作/角色) 写脚本。
    - "禁止"只剩 detail/features 未明确提供的具体属性 (材质/品牌/价格/型号),
      由 validate_script_quality 把守。

    v3.7: 6 行 -> 1 行, 移除反例字符串 "女鞋/初夏/通勤/..." —
    v3.6 反而把这段自身当成了任务, 模型拷贝反例作为正文开头。
    简短一行 "不要原文复述" 已够, 不需要举例。

    Returns: 一段直接 append 到 prompt 末尾的字符串。
    """
    # v3.7: 6 行 -> 1 行, 移除反例字符串 (v3.6 反而被模型拷贝)
    return (
        "\n【能力摘要 - 内部参考, 不要原文复述】" + capability_summary
        + " | 用例: 特写/通勤/自然光"
    )


def _build_prompt(topic: str, style: str,
                  detail: Optional[str] = None,
                  features: Optional[str] = None,
                  lang: str = "zh",
                  extra_instruction: Optional[str] = None,
                  variation_index: int = 0,
                  target_chars_min: Optional[int] = None,
                  target_chars_max: Optional[int] = None,
                  target_duration_min: Optional[float] = None,
                  target_duration_max: Optional[float] = None,
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
        target_duration_min/max: 目标时长范围（秒），用于在 prompt 头部
            把硬编码的 "30-60 秒" 替换成实际值。None 时回退 30-60s。
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
        # v3.6 修 1: 改用实际 target_duration_min/max, 不能再硬编码 30-60 秒
        # (用户配 25-30 秒时, 提示词和实际不符会让 AI 偏向写 30-60s 的长度)
        if target_duration_min is not None and target_duration_max is not None:
            _dur_lo = max(1, int(round(target_duration_min)))
            _dur_hi = max(_dur_lo, int(round(target_duration_max)))
            _dur_str = f"{_dur_lo}-{_dur_hi} 秒"
        else:
            _dur_str = "30-60 秒"
        template_block = (
            f"围绕「{topic}」自由创作一段 {_dur_str} 的口播文案。\n"
            f"不要使用任何占位符（如【】、{{}}、xx、XX），所有内容必须围绕 {topic} 写实。"
        )

    # ── 多样性约束：每条文案的开场句式 + 情绪角度必须不同 ──
    angle = _ANGLES[variation_index % len(_ANGLES)]
    diversity_hint = (
        f"\n【多样性】\n"
        f"- 角度: {angle}\n"
        f"- 开场句/句式/收尾都不得与本批次其他文案相同或套用'姐妹们/家人们/真的太香了'等模板化表达\n"
        f"- 同一短语在文中 <=1 次, 数字/促销词不重复堆叠\n"
    )

    # v3.2: 【禁止捏造设计过程 / 过度承诺 / 跨品类】 — 始终启用, 与 validation 对应
    # 极简措辞以避免 prompt 过长 (test_core_unit 期望 < 1000 字符)
    # v3.7 合并: 视觉缺失 + 禁止捏造 + 反泄漏 3 段 -> 1 段【禁止】
    no_fabrication_hint = (
        f"\n【禁止】违反任一会被 validate_script_quality 拒收:\n"
        f"1) 不编造外观 (颜色/尺寸/材质/配件), 没提供 detail/features 时只写情绪/场景/身份认同\n"
        f"2) 不编造设计过程/设计师故事 (匠心/手工/精雕细琢/设计师/灵感/反复打磨等)\n"
        f"3) 不写无支撑的过度营销词 (完美展现/完美呈现/完美融合/绝佳/艺术品/极致/独一无二/殿堂级/顶配/全球首发/颠覆性)\n"
        f"4) 不跨品类 (鞋类不能写 衣/裤/裙/包/帽; 反之亦然)\n"
    )

    # ── 长度硬约束 + 输出格式硬约束 ──
    # v3.1: 把长度要求升级为"系统会强制 trim 超过 max 的部分，少于 min 拒收"
    #       + 显式禁止 hashtag、emoji、markdown、前缀导语（之前在要求列表里已有，
    #       但模型依旧会输出，这次提到【字数硬性要求】同一段强化权重）。
    length_hint = ""
    if target_chars_min and target_chars_max:
        # v3.4 重写: 之前 v3.2 用「目标 X 字 + 范围 Y-Z」模型还是经常偏离 (实测 Qwen
        # 命中率 ~30%)。改用「精确目标 + 4 句结构 + few-shot 示例 + [字数:XXX] 自检行」,
        # 实测 DeepSeek 和本地模型都按结构输出, _clean_result 会自动剥 [字数:XXX] 标记。
        _target_ideal = (target_chars_min + target_chars_max) // 2
        _per_sentence_min = max(18, (_target_ideal - 10) // 4)
        _per_sentence_max = (_target_ideal + 12) // 4
        _num_sentences = 4
        # 4 句通用示例, 让模型照搬结构 (不照搬内容, topic 由上方 prompt 给出)。
        # 总长 118, 适配 target_ideal=120 附近的常见 30-60s 视频。
        _EX1 = "想为日常穿搭多一点灵感, 其实一双合适的鞋就能带来很大的变化。"
        _EX2 = "春夏季节穿上轻便的款式, 整体造型也会跟着松弛自然起来。"
        _EX3 = "百搭的设计不挑衣服也不挑场合, 通勤逛街约会都能轻松切换。"
        _EX3 = "百搭的设计不挑任何风格, 通勤逛街约会都能轻松切换。"
        _EX4 = "用舒服的步调走出自己的味道, 让每个普通一天都多一点新鲜感。"
        # v3.4.1: 4 句示例中第 1 句用 {topic} 占位, 强迫模型把实际 topic 词织进文案,
        # 否则 1 句 4 段示例会让模型照搬结构, 但漏掉「正文未围绕选题」检查需要的 topic 词。
        # 3-4 句保持抽象, 避免和 topic 关键词冲突触发跨品类检查。
        # v3.6 修 2: _EX1 (首句) 必须随 variation_index 轮换。
        # 旧版固定 8 个 variation_index 全用同一个
        # "想为日常穿搭多一点灵感, 其实{topic}就能带来很大的变化。",
        # 5 条文案全抄同一首句, "禁止套用开场" hint 压不住 few-shot 复制惯性。
        # 现在 8 组不同首句, 配合更强的 anti-copy 提示, 每条文案的开场必须明显不同。
        _OPENERS = [
            # 0: 反差/震惊
            "没想到{topic}还能这样, 真的是打开新世界了。",
            # 1: 限时/稀缺
            "这个季节真的强烈推荐{topic}, 错过又要等一年。",
            # 2: 痛点->方案
            "以前每次换季都头疼, 直到遇见{topic}才彻底解决。",
            # 3: 数据/对比
            "对比了十款, 最后还是{topic}最值得入手。",
            # 4: 反问/调侃
            "姐妹们, 你们还在为穿搭烦恼吗? 其实{topic}就够了。",
            # 5: 生活片段
            "那天逛街, 朋友的一句话让我重新认识了{topic}。",
            # 6: 连续追问
            "为什么很多人都在买{topic}? 因为它真的解决了三个问题。",
            # 7: 旧 vs 新
            "以前的鞋子又闷又磨脚, 换了{topic}之后走路都变轻盈。",
        ]
        # v3.6 修 2: 3-4 句也轻微轮换, 避免 5 条文案的中段也雷同
        _MID_VARIANTS = [
            [
                "春夏季节一双合适的鞋, 能让整个人的状态都松弛自然起来。",
                "百搭的设计不挑任何风格, 通勤逛街约会都能轻松切换。",
                "用舒服的步调走出自己的味道, 让每个普通一天都多一点新鲜感。",
            ],
            [
                "穿上它整个人的气质都提升了一个档次。",
                "不管是上班通勤还是周末出游, 都能轻松驾驭。",
                "那种从脚底升起的舒适感, 一整天都不会累。",
            ],
            [
                "轻盈的鞋面透气不闷脚, 走多远都不觉得累。",
                "经典的版型怎么搭配都不会出错, 实用性拉满。",
                "每一次低头看它, 都觉得今天的心情也跟着变好。",
            ],
        ]
        _EX1 = _OPENERS[variation_index % len(_OPENERS)]
        _mid = _MID_VARIANTS[variation_index % len(_MID_VARIANTS)]
        _EX2, _EX3, _EX4 = _mid[0], _mid[1], _mid[2]
        # v3.8 优化: 删 [字数:XXX] marker (系统不用, 反而占字数),
        # 加精确目标 "{target_ideal} 字" 减少 ambiguity
        # 范围 107-156 → 直接说 "目标 124 字" (用户更清楚要打多少字)
        length_hint = (
            f"目标 **{_target_ideal} 字** (范围 {target_chars_min}-{target_chars_max}, "
            f"偏差 ±15% 仍可接受)。\n"
            # v3.13 改 B: 加 "不要在 1-2 句后就结束" 强提示 (用户反馈
            # Qwen 0.5B 经常 30/60/78 字就停 — 全是 1-2 句).
        f"⚠️ 至少 **4 句** (4 个句号), **不要在 1-2 句后就结束** "
        f"— 短于 80 字 = 不合格。\n"
        f"\n【参考结构 — 3 句共 ~{_target_ideal-30} 字】\n"
        f"\"{_EX1}\" (类似开场)\n"
        f"\"{_EX2}\" (场景/细节展开)\n"
        f"\"{_EX3}\" (情绪/行动收尾)\n"
        # v3.16: 把"不写"换成显式字数计算规则, 解决"模型按全部字符数 / 系统按
        # 清洗后字符数"导致的偏差 (hashtag/emoji/markdown 不算但模型会算)。
        # 给出具体对照示例, 让模型在生成时就按系统规则计数。
        f"**字数计算规则** (与系统校验完全一致, 严格按此):\n"
        f"- 算: 中文字 + 标点 (。！？, ,) + 字母 + 数字\n"
        f"- **不算**: 空格 / 换行 / hashtag (#xxx) / emoji / markdown 标记 / 方括号元描述\n"
        f"- 也就是说: 你写的每 1 个中文字/标点都算 1 字; 写了 #标签 / emoji / markdown\n"
        f"  不会被算进字数, 写出来反而白白占位置, 一定要避免。\n"
        f"- 对照示例: 「一穿上就放不下, 春夏的女鞋真的能改变整身穿搭。\"\n"
        f"  = **31 字** (中文+标点都算; 没有 # 标签 / emoji / 空格 / markdown)\n"
        f"- 常见误区: 「#经典单品 #品质保证」写出来占 12 字符, 系统只按 0 字算。\n"
        f"  千万不要靠加 hashtag 凑字数。\n"
        f"**不写**前缀导语 / #标签 / emoji / markdown / 方括号元描述"
    )
    else:
        length_hint = "\n文案长度：5-8 句话，100-200 字。"
    # v3.4.1: 把示例里 {topic} 占位替换为实际 topic 词,
    # 让模型照搬结构时自然把 topic 织进文案, 避免「正文未围绕选题」误伤
    length_hint = length_hint.replace("{topic}", topic)
    # 示例里的句 1 也需要替换, 让 char count 准确反映实际句长
    # (不能直接 .format 因为 prompt 里其他 {} 会被误处理, replace 更安全)

    video_type_hint = ""
    if video_type:
        from autokat.core.ai_providers import video_type_prompt_hint
        video_type_hint = "\n" + video_type_prompt_hint(video_type) + "\n"

    return f"""{style_info['prompt']}

{video_type_hint}
{template_block}

要求:
1. 口语化、有感染力、适合短视频平台
2. {length_hint}
3. 围绕主题({topic}), 不重复同批其他文案的句式
{diversity_hint}{no_fabrication_hint}
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
            # v3.3: 空 choices 不再静默返回 None, 显式 raise 提示用户检查
            # 模型名/Key 权限 (用户报告: deepseek-v4-flash 返回 0 字符误以为网络问题)
            choices = result.get("choices") or []
            if not choices:
                body_excerpt = json.dumps(result, ensure_ascii=False)[:200]
                raise RuntimeError(
                    f"DeepSeek API 返回空 choices 字段 (模型 {effective_model} 可能无效或 Key 无权访问。"
                    f"DeepSeek 官方仅支持 deepseek-chat / deepseek-reasoner)。"
                    f"响应: {body_excerpt}"
                )
            # v3.3: 优先取 content; 如果是推理模型 (deepseek-reasoner /
            # deepseek-v4-flash 等带 reasoning_content 的) content 经常是空,
            # 全部 token 被 reasoning_content 吃光, 退回到 reasoning_content
            # 让上游至少拿到「思考结论」(实测: max_tokens=15 时
            # deepseek-v4-flash 返回 content="" + reasoning_content=... 整段思考)
            message = choices[0].get("message", {}) or {}
            content = message.get("content") or ""
            if not content:
                reasoning = message.get("reasoning_content") or ""
                if reasoning:
                    print(
                        f"[DeepSeek] 警告: 模型 {effective_model} 是推理模型,"
                        f"content 为空但 reasoning_content 有 {len(reasoning)} 字符,"
                        f"临时回退使用 (建议 max_tokens >= 4096)"
                    )
                    content = reasoning
            if not content:
                body_excerpt = json.dumps(result, ensure_ascii=False)[:200]
                raise RuntimeError(
                    f"DeepSeek API 返回 choices 但 message.content 为空 "
                    f"(模型 {effective_model} 触发了内容过滤或返回异常结构)。"
                    f"响应: {body_excerpt}"
                )
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
    except RuntimeError:
        # v3.3: 业务异常 (空 choices / 空 content) 不应被吞, propagate 给上游
        # 让用户在 UI 日志里看到具体原因 (模型名/Key/响应片段)
        raise
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
    # 2b. v3.4: 主动剥除模型按 prompt 要求自检添加的 [字数:XXX] / (字数:XXX) /
    #     （字数：XXX） 标记行。系统读这个标记, 验证完后从最终输出里删掉。
    #     模式: 可含中英文括号/冒号, 可带 "字数/字符数/长度/length/chars" 等同义词。
    _self_count_re = re.compile(
        r'[\[【（(]\s*(?:字数|字符数|字符|长度|length|chars?)\s*[:：]?\s*\d{1,4}\s*[\]】）)]\s*[\n。]?',
        re.IGNORECASE,
    )
    text = _self_count_re.sub('', text)
    # 整行只有 "字数: 130" 这种纯标记行 (没括号), 兜底剥掉
    text = re.sub(
        r'^\s*(?:字数|字符数|字符|长度|length|chars?)\s*[:：]?\s*\d{1,4}\s*$',
        '', text, flags=re.MULTILINE | re.IGNORECASE,
    )
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


# v3.16: 字数根因彻底修复 — 字数计算双层防御 ───────────────────────
# 用户报告 (任务 568 follow-up):
#   模型输出 75 字 (含 #经典单品 等 hashtag) → _clean_result 剥 hashtag
#   后只剩 67 字 → 触发「字数不足: 67 < 107」拒收, 3 次 retry 都救不回来。
#
# 根因 (证据 — autokat/core/writer.py 第 763 行 #\S+ 剥 hashtag):
#   模型按"全部字符"计数 (含 hashtag/emoji/markdown/方括号/空格/换行)
#   系统按"清洗后字符"计数 (去掉上面所有)
#   差距通常 4-15 字; 当模型输出接近下限时, 清洗后必然掉到下限之下。
#
# 方案 = 两层防御:
#   1. [Prompt 层] 在 _build_prompt 里写清"字数 = 字符数 (不含 hashtag/
#      emoji/markdown/方括号/空格/换行)" + 给出具体对照示例, 让模型在
#      生成时就按系统规则计数, 减少偏差。
#   2. [后处理层] 主循环重试耗尽后, 用 _post_extend_if_short 调用模型
#      做"聚焦扩写" (与主 prompt 解耦, 不混入其他要求), 用更长版本
#      替换原版, 把字数不达标问题兜底修掉。
# ──────────────────────────────────────────────────────────────────

def _build_extend_prompt(
    text: str,
    gap: int,
    target_min: int,
    target_max: int,
    topic: str,
) -> str:
    """v3.16: 聚焦「扩写」prompt, 与主生成 prompt 解耦.

    与 _build_prompt 的 EXTEND hint 不同:
      - 主 prompt 里 EXTEND hint 混在生成要求里, 模型常常忽略直接重写
      - 本 prompt 是纯扩写任务, 没有"重写"的选项, 模型只能添加新内容
    """
    _max_new = max(1, gap + 10)  # 多给 10 字 buffer, 防止清洗后还差几字
    return (
        f"你是一个文案续写助手。给你一段已生成的带货文案, 请在末尾自然衔接 1-3 句, "
        f"让总字数达到 {target_min}-{target_max} 字。\n"
        f"\n"
        f"【字数规则 — 严格按此计算, 与系统校验完全一致】\n"
        f"- 字数 = 字符数 (中文字 + 标点 + 字母 + 数字)\n"
        f"- 不计: 空格 / 换行 / hashtag (#) / emoji / markdown 标记 / 方括号元描述\n"
        f"- 也就是说: 你写的中文/标点/字母/数字都算字数; #hashtag 和 emoji 会被忽略不算\n"
        f"\n"
        f"【原始文案 (已清洗, 当前 {_content_char_count(text)} 字, 还差 {gap} 字)】\n"
        f"```\n{text}\n```\n"
        f"\n"
        f"【要求】\n"
        f"1. 只在末尾添加 1-3 句 (约 {_max_new} 字), 不要修改或重写前面的内容\n"
        f"2. 风格保持一致, 话题围绕: {topic}\n"
        f"3. 不要写: 前缀导语 / #标签 / emoji / markdown / 方括号元描述 / 数字编号\n"
        f"4. 输出: 完整文案 (原文 + 新增内容, 中间用句号/逗号自然衔接), 不要写任何解释\n"
        f"\n"
        f"请开始续写 (只输出完整文案, 不要解释)。"
    )


def _post_extend_if_short(
    text: str,
    target_min: int,
    target_max: int,
    topic: str,
    provider_obj,
    max_extend_attempts: int = 2,
) -> tuple:
    """v3.16: 后处理自动扩写 — 字数兜底修复.

    当主生成 prompt 的所有 retry 都用完, 仍然字数不足时调用。
    与主循环的 EXTEND hint 不同:
      - 本函数聚焦且独立: prompt 只问"扩写", 不混入其他质量要求
      - 用 _clean_result 清洗 extend 输出, 比较清洗后字数
      - 取清洗后字数更多的版本 (避免被模型"重写缩水")
      - 最多 max_extend_attempts 次, 失败返回原文本, 不抛异常

    Returns:
        (final_text, final_count, extend_attempts_made)
    """
    if not text or not target_min:
        return text, _content_char_count(text or ""), 0

    best_text = text
    best_count = _content_char_count(text)

    # v3.16.1: 用 model_calls 单独记录实际调用的 model 次数, 语义清晰
    # (attempt_idx - 1 在 "raw 空 / clean 空 / 异常" 路径会少算 1 次)
    model_calls = 0
    for attempt_idx in range(1, max_extend_attempts + 1):
        # 已经达标就直接返回 (避免不必要调用)
        if best_count >= target_min:
            return best_text, best_count, model_calls

        gap = target_min - best_count
        extend_prompt = _build_extend_prompt(
            text=best_text, gap=gap,
            target_min=target_min, target_max=target_max,
            topic=topic,
        )
        model_calls += 1
        try:
            raw_extended = provider_obj.generate(extend_prompt, max_tokens=512)
        except Exception as _call_err:
            # 网络/瞬时错误: 记录日志返回原文本, 不阻断主流程
            print(
                f"[文案后处理] post-extend attempt {attempt_idx} 调用失败: {_call_err}"
            )
            return best_text, best_count, model_calls

        if not raw_extended:
            print(f"[文案后处理] post-extend attempt {attempt_idx} 返回空, 停止")
            return best_text, best_count, model_calls

        # 必须 _clean_result: 模型可能又写了 hashtag/emoji/元描述, 这些
        # 不算字数, 反而把清洗后字数拉低
        cleaned_extended = _clean_result(raw_extended, topic=topic)
        if not cleaned_extended:
            print(
                f"[文案后处理] post-extend attempt {attempt_idx} 清洗后为空 (纯元描述), 停止"
            )
            return best_text, best_count, model_calls

        new_count = _content_char_count(cleaned_extended)
        old_count = _content_char_count(best_text)

        # 关键决策: 清洗后字数更多的才采纳, 否则保留旧版
        if new_count > old_count:
            best_text = cleaned_extended
            best_count = new_count
            print(
                f"[文案后处理] post-extend attempt {attempt_idx}: "
                f"{old_count} → {new_count} 字 (gap {gap} → {max(0, target_min - new_count)})"
            )
        else:
            # 模型重写后字数不增反减, 不采纳
            print(
                f"[文案后处理] post-extend attempt {attempt_idx}: "
                f"清洗后 {new_count} 字 ≤ 当前 {old_count} 字, 不采纳, 停止"
            )
            break

    return best_text, best_count, model_calls

_META_REPLY_PATTERNS = (
    "请告诉我", "请提供", "需要更多信息", "需要您提供", "想要的主题",
    "我可以为您", "我能为您", "这样我就能", "作为ai", "作为 AI",
    "无法生成", "不能生成", "抱歉",
)
# v3.7 软化: "透气/舒适/百搭" 等是鞋类**通用属性**,
# 旧版无差别禁止让用户空 detail 时几乎无字可写, 反复 fail 浪费时间。
# 现在只保留"硬数据" (具体颜色/型号) 和"功能卖点" (防水/抗菌/防滑/耐磨/矫正 等易编的)。
_UNSUPPORTED_PRODUCT_CLAIMS = (
    "面料", "材质", "环保", "真皮", "皮革", "棉质",
    "增高", "矫正", "按摩", "抗菌",
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
# v3.11: 用户反馈"完美之类的形容词可以放行, 允许使用"
# 只保留 3 个最 clear 的 overclaim (艺术品/颠覆性/革命性)
# 其他 10 字 (完美展现/极致/独一无二/绝佳/殿堂级/顶配/全新升级/全球首发/完美呈现/完美融合) 放行
# 理由: 这些是营销常用形容词, 模型自然使用, 不应 reject
# (prompt 仍然提示模型避开, 但 validation 不强制 reject)
_OVERCLAIMS_NO_SUPPORT = (
    "艺术品", "颠覆性", "革命性",
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
    # v3.4.2 修: 之前只加 bigram (2 字) 但过滤掉 1 字, "鞋" 漏。
    # 真正修法: 把每个单字也加进 terms (e.g. "时尚女鞋" -> "鞋")
    if len(compact) >= 1:
        terms.extend(compact[i] for i in range(len(compact)))
    # v3.4.2: 也保留 1 字片段 (e.g. topic "时尚女鞋" 的 "鞋" 单独 1 字),
    # Qwen 0.5B 经常输出 "一双合适的鞋" 而不是 "时尚女鞋", 之前过滤掉 1 字
    # 导致 "正文未围绕选题" 误伤。现在 1 字也算 on-topic 片段。
    # 注意: 这会降低 topic 检查严格度, 但跨品类/字数/质量检查仍守住底线,
    # 即便纯写 "鞋子" 也会被后续的过短/过简检查捕获。
    return list(dict.fromkeys(term for term in terms if len(term) >= 1))


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
    # v3.8 优化: 字数 ±15% 容差 (用户反馈太严, 偏差不大应接受)
    # 107-156 的范围容许 91-179, 解决 Qwen 0.5B 反复生成 71-83 字 fail 的问题
    # 极端偏离 (<91 或 >179) 才硬拒, 避免视频明显短/长于目标
    if target_chars_min and char_count < int(target_chars_min * 0.85):
        reasons.append(f"字数不足: {char_count} < {target_chars_min} (允许 ±15%)")
    if target_chars_max and char_count > int(target_chars_max * 1.15):
        reasons.append(f"字数超限: {char_count} > {target_chars_max} (允许 ±15%)")
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
    # v3.2 优化: 跨品类混淆 — 只在「作为主语/品类描述」时才算混淆。
    # 启发式: 服装搭配场景下 (如 "搭配裤子更有型"), 提到 裤子/裙 是合理的
    # 搭配描述, 不是把鞋描述为裤子。简单 substring 匹配会误判, 用户报告频繁失败。
    # 判定规则 (按优先级):
    #   1. forbidden word 出现在 「搭配/配/和/与/跟/百搭」 后面 → 搭配描述, 不算
    #   2. forbidden word 所在子句 (按 。！？?；;，, 切分) 提到任意 topic word → on-topic 搭配, 不算
    #   3. forbidden word 所在子句完全没提 topic word → 跨品类混淆, 标记
    # 注: 用「子句」不是「句子」 — 句子以 。！？? 结束, 但一个句子里可能有
    # 多个并列子句 (以 ，, 隔开), 跨品类检查需要按子句粒度判定, 否则
    # "穿上这款女鞋, 您将不再仅仅是一件衣服" 会被误判为 on-topic。
    category = _detect_topic_category(topic)
    if category:
        forbidden = []
        for cat_keys, bad_words in _CROSS_CATEGORY_FORBIDDEN.items():
            if category in cat_keys:
                forbidden.extend(bad_words)
        _STYLING_PRE = ("搭配", "配上", "配着", "配", "和", "与",
                        "跟", "百搭", "相配", "相称", "适配")
        _CLAUSE_RE = re.compile(r"[。！？?；;，,]+")
        _clause_spans = []  # list of (start, end) for each non-empty clause
        _pos = 0
        for _m in _CLAUSE_RE.finditer(text):
            if _m.start() > _pos:
                _clause_spans.append((_pos, _m.start()))
            _pos = _m.end()
        if _pos < len(text):
            _clause_spans.append((_pos, len(text)))
        topic_terms = set(_topic_terms(topic))
        # v3.4.2 副作用: 1 字片段 (e.g. "鞋" "衣") 太泛, 跨品类检测用 1 字会
        # 误放过 (e.g. 衣橱 含 "衣" 让 "运动鞋是衣橱必备" 误判为 on-topic)。
        # 跨品类检测只用 2+ 字, 保证真跨品类 (鞋说成衣) 仍被拦。
        # 1 字片段继续用于 topic 存在性检查 ("正文未围绕选题"), 让 Qwen 0.5B
        # 输出 "合适的鞋" 不再误伤。
        _topic_terms_2plus = {t for t in topic_terms if len(t) >= 2}
        cross_hits = []
        for w in forbidden:
            if w not in text:
                continue
            flag_this = False
            for match in re.finditer(re.escape(w), text):
                # 规则 1: 前缀是搭配标记 → 算搭配描述, 跳过
                prefix = text[max(0, match.start() - 6):match.start()]
                if any(p in prefix for p in _STYLING_PRE):
                    continue
                # 规则 2: 找该 word 所在子句, 看是否提到 topic
                m_start = match.start()
                clause = ""
                for c_start, c_end in _clause_spans:
                    if c_start <= m_start < c_end:
                        clause = text[c_start:c_end]
                        break
                if not any(term in clause for term in _topic_terms_2plus):
                    flag_this = True
                    break
            if flag_this:
                cross_hits.append(w)
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
    target_duration_min: Optional[float] = None,
    target_duration_max: Optional[float] = None,
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
    # v3.3: 推理模型 (deepseek-v4-flash / deepseek-reasoner) 输出的 token
    # 会被 reasoning_content 吃掉一大半, 给足 max_tokens 让 content 也有空间。
    # 普通对话模型 (deepseek-chat / deepseek-coder) 不受影响, 多给 token 没事。
    # v3.3 用户补充 deepseek-v4-flash 官方规格: 1M 上下文 + 384K 最大输出。
    # 给 4096 tokens ≈ 12000 中文字, 远超我们实际需要的 142 字脚本, 给推理链
    # 留足空间 (实测 max_tokens=15 时全部 token 被 reasoning_content 吃光)。
    if provider == "deepseek" and _max_tokens < 4096:
        _max_tokens = 4096

    # Provider selection goes through ai_providers.build_writer_provider so
    # the macOS Keychain and ai_settings.json are the single source of truth.
    # A failed provider never silently switches to a different provider.
    from autokat.core.ai_providers import build_writer_provider
    provider_obj = build_writer_provider(provider)
    backend_name = type(provider_obj).__name__
    backends = [(backend_name, provider_obj.generate)]

    last_reasons = []

    for backend_name, call_backend in backends:
        # v3.4.3: 记录之前 attempt 的输出, 重试时传给模型让它「扩写」而不是重写。
        # Qwen 0.5B 经常 1-2 句就停 (30-60 字), 3 次重写都不会自增长。
        # 改成「在前一版基础上加内容」命中率显著更高。
        _previous_outputs = []  # list of cleaned text from past attempts
        for attempt in range(1, max(1, max_attempts) + 1):
            retry_hint = ""
            if last_reasons and _previous_outputs:
                _prev = _previous_outputs[-1]
                _prev_len = len(_prev)
                # v3.8: EXTEND 提示更具体 (告诉模型还差多少字)
                # 解决 Qwen 0.5B 反复欠字数 (71-83 字 vs 107+ 目标)
                _gap = max(1, target_chars_min - _prev_len) if target_chars_min else 0
                retry_hint = (
                    f"\n【重要 — EXTEND 扩写, 不要重写!】\n"
                    f"上一版 {_prev_len} 字, 还差 {_gap} 字 (目标 {target_chars_min}-{target_chars_max}, "
                    f"理想 {(target_chars_min + target_chars_max) // 2 if (target_chars_min and target_chars_max) else 0} 字)。\n"
                    f"请在末尾添加 1-2 句 (例: 场景细节/情绪总结/行动呼吁), "
                    f"**不要从头重写**: \n"
                    f"```\n{_prev}\n```\n"
                    f"具体未通过: {';'.join(last_reasons)}\n"
                )
            elif last_reasons:
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
                target_duration_min=target_duration_min,
                target_duration_max=target_duration_max,
                video_type=video_type,
            )
            if material_capabilities:
                # v3.5 (方案 A): 把"不得编造素材无法支持的画面"改成"可以引用这些能力"。
                # 详见 _format_capability_summary_prompt 文档。字面常量也由同函数导出,
                # 测试 (test_v35_capability_summary) 走 _format_capability_summary_prompt
                # 拿到精确字符串, 避免与运行时 prompt 漂移。
                prompt += _format_capability_summary_prompt(material_capabilities)
            if progress_callback:
                progress_callback(
                    backend_name, attempt, max_attempts,
                    "正在生成" if not last_reasons else "；".join(last_reasons),
                )
            # v3.5 后台打印: 完整 prompt 写到 stderr, 标 [writer.debug] 便于 grep。
            # AI 文案生成时方便调试 (检查切片摘要是否被引用、few-shot 是否照搬等)。
            # 默认开启; 性能开销 < 1ms/prompt (2124 chars 字符串拷贝)。
            import sys
            try:
                print(
                    f"\n[writer.debug] ===== AI PROMPT (topic={topic}, style={style}, "
                    f"target={target_chars_min}-{target_chars_max}, attempt={attempt}/{max_attempts}) "
                    f"=====\n{prompt}\n===== END PROMPT =====\n",
                    file=sys.stderr, flush=True,
                )
            except Exception:
                pass
            try:
                raw = provider_obj.generate(prompt, max_tokens=_max_tokens)
            except RuntimeError:
                # v3.3: 业务/永久错误 (模型名错, Key 无效, 推理模型 content 空
                # 被回退后仍空 等) 不应被静默重试 — 立即向上抛, 让用户看到真实
                # 原因, 不要再浪费 3 次 API 调用和用户 30 秒等待。
                # 网络/瞬时错误 (ConnectionError, URLError, TimeoutError)
                # 走下面 except Exception 路径, 仍会重试。
                # v3.3.1: 包装异常带上 provider 名 (LocalWriterProvider /
                # DeepSeekWriterProvider), 用户能直接定位是哪个后端失败。
                import sys as _sys
                raise RuntimeError(
                    f"{backend_name}: {_sys.exc_info()[1]}"
                ) from _sys.exc_info()[1]
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
            # v3.4.3: 保留 wildly_off 早 fail (模型完全跑偏, 重写也救不回来),
            # 但放宽阈值: 之前 <0.5x min 才 break, 现在 <0.3x min 才 break。
            # 50 字 / 107 字 = 0.47x 不 break, 让 EXTEND 重试有机会扩到 110+。
            # 20 字 / 107 字 = 0.19x 才 break (模型真的不懂任务)。
            # v3.4.4: 完全删除 wildly_off 早 break。EXTEND 重试的强指引
            # (前置输出 + "扩写不要重写") 比任何阈值都有效, 给模型
            # 全部 3 次机会。3 次都不行才 raise (3.2 行为不变)。
            # 之前 0.3x min 阈值在 50/107 = 0.47x 时错误地 break,
            # 让 EXTEND 提示根本没机会执行。删掉, 让流程跑完整。
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
            # v3.4.3: 记录本版 (清洗后) 输出, 下一 attempt 让模型 EXTEND 而不是重写
            if result:
                _previous_outputs.append(result)
            # v3.16: 跟踪最后一轮的清洗后结果, 主循环全部失败时走 post-extend 安全网
            _last_cleaned_result = result
            last_reasons = quality["reasons"] or ["模型未返回有效正文"]
            print(
                f"[文案质量] {backend_name} 第 {attempt}/{max_attempts} 次不合格: "
                + "；".join(last_reasons)
            )

    # v3.16: 字数根因彻底修复 — 主循环全部 retry 失败后, 启用 post-extend 安全网。
    # 原因: 模型按"全部字符"计数 (含 hashtag/emoji/markdown/方括号/空格), 系统按
    # "清洗后字符"计数, 清洗后通常少 4-15 字。当模型输出接近下限时, 必然掉到下限
    # 之下, 3 次重写都救不回来。post-extend 用聚焦"扩写"prompt 让模型在已知 result
    # 上加内容, 解决"模型/系统字数规则不一致"导致的字数不足。
    if (
        _last_cleaned_result
        and target_chars_min
        and target_chars_max
        and _content_char_count(_last_cleaned_result) < target_chars_min
    ):
        try:
            _post_text, _post_count, _post_attempts = _post_extend_if_short(
                text=_last_cleaned_result,
                target_min=target_chars_min,
                target_max=target_chars_max,
                topic=topic,
                provider_obj=provider_obj,
            )
            if _post_text != _last_cleaned_result and _post_count >= target_chars_min:
                _post_quality = validate_script_quality(
                    _post_text, topic, lang=lang,
                    target_chars_min=target_chars_min,
                    target_chars_max=target_chars_max,
                    detail=detail, features=features,
                    accepted_texts=accepted_texts,
                )
                if _post_quality["valid"]:
                    print(
                        f"[文案后处理] post-extend 救场成功 ({_post_attempts} 次), "
                        f"最终 {_post_count} 字 (>= {target_chars_min})"
                    )
                    return {
                        "text": _post_text,
                        "source": backend_name,
                        "quality": _post_quality,
                    }
                else:
                    print(
                        f"[文案后处理] post-extend 救场后仍不合格: "
                        + "；".join(_post_quality["reasons"])
                    )
        except Exception as _post_err:
            # post-extend 自身出错 (不应阻断主 raise 路径)
            print(f"[文案后处理] post-extend 抛异常 (不影响 raise): {_post_err}")

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
