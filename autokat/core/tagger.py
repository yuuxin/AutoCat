"""素材自动打标 + 文案智能匹配

用 CLIP 模型提取图片特征向量，结合关键词标签，实现素材与文案的智能匹配。

架构：
- 素材入库时：用 CLIP 提取特征 + 规则关键词打标
- 混剪时：分析文案关键词 → 匹配最佳素材
- 全部本地运行，无云端依赖
"""

import json
import re
from autokat.models.db import get_all_materials

# ── 预定义标签库 ──
# 每个标签对应的中文关键词（用于文案匹配）
TAG_KEYWORDS = {
    "人物": ["人", "人物", "肖像", "脸", "表情", "模特", "博主", "主播", "自拍", "合照"],
    "产品": ["产品", "商品", "物品", "实物", "展示", "开箱", "包装", "细节"],
    "风景": ["风景", "自然", "户外", "天空", "山", "水", "海", "日落", "日出", "夜景"],
    "食物": ["食物", "美食", "吃", "烹饪", "食材", "料理", "甜品", "饮品"],
    "室内": ["室内", "房间", "客厅", "卧室", "厨房", "书房", "家居", "装修"],
    "科技": ["科技", "数码", "手机", "电脑", "软件", "app", "应用", "智能"],
    "运动": ["运动", "健身", "跑步", "瑜伽", "锻炼", "体育", "户外活动"],
    "教育": ["教育", "学习", "课程", "知识", "教学", "教程", "技巧", "方法"],
    "时尚": ["时尚", "穿搭", "服装", "配饰", "美妆", "护肤", "发型"],
    "旅行": ["旅行", "旅游", "出行", "打卡", "探店", "景点", "攻略"],
    "生活": ["生活", "日常", "vlog", "记录", "分享", "好物", "收纳", "整理"],
    "宠物": ["宠物", "猫", "狗", "动物", "萌宠", "狗狗", "猫咪"],
    "艺术": ["艺术", "设计", "创意", "绘画", "插画", "视觉", "美学"],
    "文字": ["文字", "标题", "文案", "字幕", "卡片", "quote", "名言"],
    "特写": ["特写", "细节", "微距", "局部", "近距离"],
    "远景": ["远景", "大全景", "航拍", "俯瞰", "广角"],
}

# 图片特征缓存

# ── 文案素材匹配 ──

def extract_keywords(text: str) -> list[str]:
    """从文案中提取关键词"""
    # 简单分词：提取中文词
    words = set()
    for tag, keywords in TAG_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                words.add(tag)
                break

    # 如果没有任何关键词匹配，返回通用标签
    if not words:
        words.add("生活")

    return list(words)


def match_materials_for_text(text: str, top_k: int = 20) -> list[dict]:
    """根据文案内容匹配合适的素材

    Args:
        text: 口播文案
        top_k: 返回前 k 个最匹配的素材

    Returns:
        [{id, path, duration, type, tags, score}, ...]
    """
    keywords = extract_keywords(text)
    materials = get_all_materials("video")  # 只选 video 类型

    scored = []
    for mat in materials:
        mat_tags = json.loads(mat["tags"] or "[]")
        # 计算标签匹配度
        matched = len(set(keywords) & set(mat_tags))
        score = matched / max(len(keywords), 1)

        # 如果素材有特征向量，结合向量相似度
        # (二期 CLIP 加载后可加)
        scored.append({
            "id": mat["id"],
            "path": mat["file_path"],
            "duration": mat["duration"],
            "type": mat["mat_type"],
            "tags": mat_tags,
            "score": score,
        })

    # 按匹配度排序
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]



