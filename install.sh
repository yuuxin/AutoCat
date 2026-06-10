#!/usr/bin/env bash
# ============================================
# AutoCat 一键安装脚本
# 低成本单机智能批量混剪系统 v3.0
# ============================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ── 检测系统 ──
OS="$(uname -s)"
ARCH="$(uname -m)"

echo ""
echo "============================================"
echo "   AutoCat v3.0 一键安装"
echo "   支持 macOS / Linux"
echo "============================================"
echo ""

# ── 检测 Python ──
PYTHON=""
for cmd in python3 python3.11 python3.10; do
    if command -v $cmd &>/dev/null; then
        VER=$($cmd --version 2>&1 | grep -oP '\d+\.\d+' | head -1)
        if [ "$(echo "$VER >= 3.10" | bc)" = "1" ] 2>/dev/null || [ "$VER" = "3.10" ] || [ "$VER" = "3.11" ] || [ "$VER" = "3.12" ] || [ "$VER" = "3.13" ] || [ "$VER" = "3.14" ]; then
            PYTHON=$cmd
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    error "需要 Python >= 3.10"
    echo "  安装: brew install python (macOS) 或 apt install python3 (Linux)"
    exit 1
fi
info "Python: $($PYTHON --version)"

# ── 检测/创建虚拟环境 ──
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d ".venv" ]; then
    info "创建虚拟环境..."
    $PYTHON -m venv .venv
fi
source .venv/bin/activate
info "虚拟环境: $(which python)"

# ── 安装依赖 ──
info "安装 Python 依赖（首次可能需要 3-5 分钟）..."
pip install --upgrade pip -q
pip install -e . -q

# ── 检测 FFmpeg ──
FFMPEG_OK=false
if command -v ffmpeg &>/dev/null; then
    FFMPEG_OK=true
    info "FFmpeg: $(ffmpeg -version 2>&1 | head -1)"
else
    warn "未检测到 FFmpeg"
fi

# 检查 drawtext filter
if $FFMPEG_OK; then
    if ffmpeg -filters 2>/dev/null | grep -q drawtext; then
        info "✅ FFmpeg 支持 drawtext 字幕"
    else
        warn "FFmpeg 不支持 drawtext 字幕"
        if [ "$OS" = "Darwin" ]; then
            echo "  建议安装 ffmpeg-full: brew install ffmpeg-full"
        elif command -v apt &>/dev/null; then
            echo "  建议: apt install ffmpeg libavfilter-dev libfreetype-dev"
        fi
    fi
fi

# ── 创建目录结构 ──
mkdir -p assets/{images,videos,kenburns,clips,tts,bgm}
mkdir -p tasks/scripts output

# ── 初始化数据库 ──
python -m autokat init 2>/dev/null || true

echo ""
info "✅ AutoCat v3.0 安装完成！"
echo ""
echo "  📂 工作目录: $SCRIPT_DIR"
echo ""
echo "  使用方法:"
echo "    source .venv/bin/activate         # 激活环境"
echo "    autokat init                      # 初始化"
echo "    autokat import 图片/视频...        # 导入素材"
echo "    autokat generate '文案' 100       # 生成 100 条"
echo "    autokat resume                    # 中断续跑"
echo "    autokat ui                        # 启动桌面"
echo ""
echo "  或者直接:"
echo "    cd $SCRIPT_DIR"
echo "    .venv/bin/python -m autokat ui    # 启动桌面"
echo ""
echo "  构建 macOS 安装包:"
echo "    make dmg                        # 构建 .dmg 安装包"
echo "    # 或: .venv/bin/python3 build_app.py --dmg"
echo ""
echo "  详细文档: README.md" 
echo "============================================"
