# AutoCat — 低成本单机智能批量混剪系统 v3.0

> 10 份基础素材 → 100+ 条差异化成片，全链路本地运行，零 API 费用。

基于原始素材（图片、3~5s 视频）自动混剪组合，生成适合抖音/TikTok 的竖屏短视频。适合自媒体矩阵号批量生产内容。

---

## 特性

- 🎬 **素材裂变**：视频拆子镜头 + 图片 Ken Burns 动效，少量素材产出海量成片
- 🎯 **智能匹配**：文案关键词 → 素材标签匹配，画面与内容更贴合
- 🎵 **BGM 混配**：librosa 节拍检测，自动裁剪循环
- 🎨 **18 种滤镜 + 18 种转场**：每段视频随机组合，确保差异化
- 🤖 **AI 写文案**（双模式可选）：本地 Qwen2.5-0.5B 离线生成，或配置 DeepSeek API 云端生成（效果更好）
- 🔑 **DeepSeek 选配**：设置 `DEEPSEEK_API_KEY` 环境变量或 UI 中输入 Key 即可启用，文案质量更高
- 📤 **多平台导出**：一键适配抖音/TikTok/快手/小红书/B站
- 🔍 **感知哈希去重**：自动剔除相似度 >85% 的重复成片
- ⏯ **中断续跑**：批量生成中断后自动恢复
- 🖥 **桌面 UI**：PySide6 图形界面，小白友好
- 💰 **零 API 费用**：全链路本地运行，无需联网

---

## 硬件要求

| 档位 | 配置 | 产能 |
|------|------|------|
| 入门 | i5/R5, 8GB 内存, 无独显 | 20~50 条/批 |
| 推荐 | i7/R7, 16GB 内存 | 100+ 条/批 |
| AI 文案 | 额外 4GB 内存（Qwen 模型） | 可选 |

---

## 快速安装

> 面向普通用户分发的自包含 macOS DMG 正在按新的发布标准重建。当前仓库中的旧打包脚本仅用于本地开发验证，不应生成对外正式安装包。详见 [macOS 打包与发布标准](docs/MACOS_PACKAGING_STANDARD.md)。

### 前置依赖

```bash
# macOS
brew install ffmpeg       # 基础 FFmpeg
brew install ffmpeg-full  # 推荐，支持字幕叠加
```

### 一键安装

```bash
git clone <仓库地址>
cd AutoCat
chmod +x install.sh
./install.sh
```

### 或手动安装

```bash
# 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 安装核心依赖
pip install -e .


### 选配：DeepSeek API（推荐，文案质量更好）

两种方式配置：

**方式一：环境变量**
```bash
export DEEPSEEK_API_KEY=sk-your-key-here
autokat ui
```

**方式二：UI 界面配置**
1. 启动桌面 UI → 点击「🤖 AI 写文案」标签
2. 在「DeepSeek API 配置」框中输入你的 API Key
3. 点击「保存 Key」，立即生效

获取 DeepSeek API Key：[platform.deepseek.com](https://platform.deepseek.com/api_keys)

> 未配置时自动使用本地 Qwen-0.5B 模型（需首次下载 ~1GB），或回退到模板文案。


# 如需 AI 文案功能（可选，首次加载需下载 ~1GB 模型）
pip install -e ".[ai]"
```

---

## 使用方式

### 命令行

```bash
source .venv/bin/activate

# 初始化数据库
autokat init

# 导入素材（支持 jpg/png/webp/mp4/mov）
autokat import 素材1.jpg 素材2.mp4 ...

# 批量生成 100 条视频
autokat generate "你的口播文案" 100

# 中断后续跑
autokat resume

# 启动桌面 UI
autokat ui
```

### 桌面 UI

```bash
autokat ui
```

打开 UI 后：
1. **📦 素材管理** → 导入图片/视频素材
2. **📝 文案配音** → 输入口播文案，选择音色
3. **🚀 批量生成** → 设置数量/并发，点击生成
4. **📤 导出** → 一键导出到多平台

---

## 项目结构

```
AutoCat/
├── autokat/                 # 核心代码
│   ├── __main__.py          # CLI 入口
│   ├── core/
│   │   ├── material.py      # 素材导入/预处理/Ken Burns
│   │   ├── tts.py           # 配音/字幕/SRT
│   │   ├── editor.py        # 编排引擎（18 滤镜/18 转场）
│   │   ├── renderer.py      # FFmpeg 渲染管线
│   │   ├── tagger.py        # 自动打标/智能匹配
│   │   ├── dedup.py         # 感知哈希去重
│   │   ├── bgm.py           # BGM 管理/节拍检测
│   │   ├── writer.py        # Qwen AI 文案生成
│   │   └── exporter.py      # 多平台导出
│   ├── models/
│   │   └── db.py            # SQLite 数据库
│   └── ui/
│       └── main_window.py   # PySide6 桌面 UI
├── assets/                  # 素材资源目录
│   ├── images/              # 标准化后的图片
│   ├── kenburns/            # Ken Burns 动效视频
│   ├── clips/               # 视频子镜头
│   ├── tts/                 # 配音音频
│   └── bgm/                 # BGM 音乐
├── output/                  # 生成视频输出
├── tasks/                   # 任务/脚本持久化
├── install.sh               # 一键安装脚本
├── pyproject.toml           # 项目配置
└── README.md
```

---

## 技术栈

| 组件 | 用途 | 许可证 |
|------|------|--------|
| FFmpeg | 视频渲染引擎 | LGPL |
| PySide6 | 桌面 UI | LGPL |
| Edge-TTS | 免费配音 | MIT |
| librosa | BPM 节拍检测 | ISC |
| Qwen2.5-0.5B | AI 文案（可选） | Apache 2.0 |
| imagehash | 感知哈希去重 | MIT |
| SQLite | 本地数据库 | Public Domain |

---

## 开发计划

- [x] 一期：素材导入、TTS 配音、基础编排、渲染管线
- [x] 二期：智能打标、去重、BGM 混配、扩充滤镜
- [x] 三期：AI 文案、多平台导出、多进程优化、安装包
- [ ] 未来：AI 画面生成（Stable Diffusion）、音色克隆、API Server

---

**License**: MIT
