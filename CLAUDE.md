# AutoCat — 低成本单机智能批量混剪系统

> 10 份基础素材 → 100+ 条差异化成片，全链路本地运行，零 API 费用。

## 项目概述

AutoCat 是一个基于 FFmpeg + AI 的短视频批量生产工具，支持素材裂变、智能匹配、BGM 混配、AI 文案生成等功能。

## 技术栈

- **语言**: Python 3.10+
- **核心依赖**: PySide6, FFmpeg, edge-tts, librosa, Pillow, imagehash
- **可选 AI**: Qwen2.5-0.5B (本地), DeepSeek API (云端)
- **数据库**: SQLite (`tasks/autokat.db`)

## 项目结构

```
AutoCat/
├── autokat/                  # 核心代码
│   ├── __main__.py           # CLI 入口 (autokat init/import/generate/resume/ui)
│   ├── core/
│   │   ├── material.py       # 素材导入/预处理/Ken Burns 动效
│   │   ├── tts.py           # 配音/字幕/SRT 生成
│   │   ├── editor.py        # 编排引擎（18 滤镜/18 转场）
│   │   ├── renderer.py      # FFmpeg 渲染管线
│   │   ├── tagger.py        # 自动打标/智能匹配
│   │   ├── dedup.py         # 感知哈希去重
│   │   ├── bgm.py           # BGM 管理/节拍检测
│   │   ├── writer.py        # AI 文案生成 (Qwen/DeepSeek)
│   │   ├── exporter.py      # 多平台导出
│   │   └── ffmpeg_utils.py  # FFmpeg 工具函数
│   ├── models/
│   │   └── db.py            # SQLite 数据库模型
│   └── ui/
│       └── main_window.py   # PySide6 桌面 UI
├── assets/                   # 素材资源目录
│   ├── images/              # 标准化后的图片
│   ├── kenburns/           # Ken Burns 动效视频
│   ├── clips/               # 视频子镜头
│   ├── tts/                 # 配音音频
│   └── bgm/                 # BGM 音乐
├── output/                   # 生成视频输出目录
├── tasks/                    # 任务/脚本持久化 + SQLite 数据库
├── build_app.py              # macOS .app 构建脚本
├── pyproject.toml           # 项目配置
└── Makefile                 # 构建命令
```

## 构建与运行

```bash
# 安装依赖
make install

# 安装 AI 可选依赖
make install-ai

# 构建 macOS .app
make app

# 清理构建产物
make clean
```

## CLI 命令

```bash
autokat init       # 初始化数据库
autokat import <files...>  # 导入素材
autokat generate "<文案>" <数量>  # 批量生成
autokat resume     # 中断后续跑
autokat ui         # 启动桌面 UI
```

## 开发约定

- **代码风格**: PEP 8，使用 ruff 检查
- **并发模型**: 多进程批量生成，通过 `tasks/scripts/` 持久化任务状态
- **素材处理**: 统一到 `assets/` 子目录，数据库记录路径
- **日志**: `logs/app.log`
- **环境变量**: `DEEPSEEK_API_KEY` (可选), `.env` 文件支持

## 关键文件

| 文件 | 用途 |
|------|------|
| `autokat/__main__.py` | CLI 入口，所有子命令定义 |
| `autokat/core/renderer.py` | FFmpeg 渲染管线，核心复杂度 |
| `autokat/core/material.py` | Ken Burns 动效、视频子镜头拆分 |
| `autokat/core/writer.py` | AI 文案生成 (Qwen 本地 / DeepSeek API) |
| `autokat/models/db.py` | SQLite ORM 模型 |
| `build_app.py` | macOS 桌面应用打包 |
