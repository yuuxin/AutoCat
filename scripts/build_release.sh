#!/usr/bin/env bash
# AutoCat 单架构 (Apple Silicon arm64) 发布构建脚本
#
# 在 Apple Silicon 主机 (macOS 14+) 上跑这一份脚本, 即可产出 release arm64 DMG.
# Intel Mac 用户走 Rosetta 2 转译, 不需要单独的 x86_64 构建.
#
# 用法:
#   scripts/build_release.sh                # 默认 auto 架构
#   scripts/build_release.sh --skip-build   # 只跑 audit + 生成 SHA256SUMS (DMG 已存在时)
#   scripts/build_release.sh --version 3.1.0  # 覆盖版本号
#
# 前置条件:
#   - Apple Silicon (M1/M2/M3/M4) 主机, macOS 14+
#   - python.org 64-bit universal2 Python 3.12.x (或同等干净的 Python framework)
#   - Homebrew FFmpeg (brew install ffmpeg-full)
#   - .venv 已建好 (make install)

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

SKIP_BUILD=false
VERSION_OVERRIDE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-build)
            SKIP_BUILD=true
            shift
            ;;
        --version)
            VERSION_OVERRIDE="$2"
            shift 2
            ;;
        -h|--help)
            head -25 "$0" | tail -20
            exit 0
            ;;
        *)
            echo "未知参数: $1" >&2
            exit 2
            ;;
    esac
done

# ── 前置检查 ──
HOST_ARCH="$(uname -m)"
if [[ "$HOST_ARCH" != "arm64" ]]; then
    echo "❌ 当前主机 $HOST_ARCH, 本脚本只支持 Apple Silicon (arm64)" >&2
    echo "   Intel Mac 用户请走 Rosetta 2 转译 arm64 .app" >&2
    exit 1
fi

if [[ ! -d ".venv" ]]; then
    echo "❌ 未找到 .venv, 请先跑: make install" >&2
    exit 1
fi

PYTHON=".venv/bin/python3"

if [[ -n "$VERSION_OVERRIDE" ]]; then
    echo "⚠️  临时覆盖版本号为 $VERSION_OVERRIDE (尚未改 pyproject.toml)"
fi

# ── 1. 构建 .app + .dmg ──
APP_NAME="AutoCat"
APP_VERSION="$($PYTHON -c 'import tomllib; print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"]')"
DMG_NAME="${APP_NAME}-${APP_VERSION}-macOS-arm64.dmg"
DMG_PATH="dist/$DMG_NAME"

if [[ "$SKIP_BUILD" == "true" ]]; then
    echo "⏭  跳过 build, 直接 audit 已存在的 dist/$APP_NAME.app"
else
    echo "▶ 构建 .app + .dmg (arch=arm64, min=14.0)"
    "$PYTHON" build_app.py --arch arm64 --dmg
fi

# ── 2. 审计 ──
echo
echo "▶ 审计 .app 包内运行时"
"$PYTHON" packaging/python_runtime_audit.py "dist/$APP_NAME.app"
AUDIT_EXIT=$?
case $AUDIT_EXIT in
    0)  echo "✅ 审计通过" ;;
    1|2|4|5|6|7)
        echo "❌ 审计失败 (exit=$AUDIT_EXIT), 修复后重跑" >&2
        exit $AUDIT_EXIT
        ;;
    3)  echo "⚠️  Python framework 来源不明, 建议改用 python.org universal2 安装" ;;
    *)  echo "❌ 审计异常 (exit=$AUDIT_EXIT)" >&2; exit $AUDIT_EXIT ;;
esac

# ── 3. 生成 SHA256SUMS ──
echo
echo "▶ 生成 SHA256SUMS"
if [[ -f "$DMG_PATH" ]]; then
    shasum -a 256 "$DMG_PATH" > "dist/SHA256SUMS"
    cat "dist/SHA256SUMS"
else
    echo "❌ 找不到 $DMG_PATH" >&2
    exit 1
fi

# ── 4. 提示后续步骤 ──
echo
echo "✅ 单架构 release 产物就绪:"
echo "   $DMG_PATH ($(du -h "$DMG_PATH" | cut -f1))"
echo "   dist/SHA256SUMS"
echo
echo "下一步 (手动):"
echo "   1. codesign --deep --force --options runtime --sign 'Developer ID Application: <TEAM>' $DMG_PATH"
echo "   2. xcrun notarytool submit $DMG_PATH --keychain-profile <PROFILE> --wait"
echo "   3. xcrun stapler staple $DMG_PATH"
echo "   4. git tag v$APP_VERSION && git push origin v$APP_VERSION"
echo "   5. gh/glab release create v$APP_VERSION $DMG_PATH#arm64"
