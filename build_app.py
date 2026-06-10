#!/usr/bin/env python3
"""AutoCat macOS .app + .dmg 打包脚本

生成标准的 macOS .app bundle，依赖系统已安装的 Python 和 FFmpeg。

用法：
    python build_app.py          # 构建 .app
    python build_app.py --dmg    # 构建 .app + .dmg
"""

import os
import sys
import shutil
import subprocess
import stat
import json
from pathlib import Path

APP_NAME = "AutoCat"
APP_VERSION = "3.0.0"
PROJECT_DIR = Path(__file__).resolve().parent
VENV_DIR = PROJECT_DIR / ".venv"
DIST_DIR = PROJECT_DIR / "dist"


def build_app(create_dmg: bool = False):
    print("=" * 60)
    print(f"AutoCat v{APP_VERSION} macOS 安装包构建")
    print("=" * 60)

    if not VENV_DIR.exists():
        print(f"❌ 未找到虚拟环境: {VENV_DIR}")
        print("请先运行: python3 -m venv .venv && source .venv/bin/activate && pip install -e .")
        sys.exit(1)

    # 获取 site-packages 路径
    result = subprocess.run(
        [str(VENV_DIR / "bin" / "python3"), "-c",
         "import site; print(site.getsitepackages()[0])"],
        capture_output=True, text=True, timeout=10,
    )
    site_packages = result.stdout.strip()

    # 获取 Python 版本
    result = subprocess.run(
        [str(VENV_DIR / "bin" / "python3"), "-c",
         "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        capture_output=True, text=True, timeout=10,
    )
    py_ver = result.stdout.strip()
    py_ver_short = py_ver.replace(".", "")

    print(f"\n[1/4] 创建 .app 目录结构...")
    app_dir = DIST_DIR / f"{APP_NAME}.app"
    contents_dir = app_dir / "Contents"
    macos_dir = contents_dir / "MacOS"
    resources_dir = contents_dir / "Resources"

    if app_dir.exists():
        shutil.rmtree(app_dir)
    for d in [macos_dir, resources_dir]:
        d.mkdir(parents=True, exist_ok=True)

    print(f"[2/4] 生成 Info.plist...")
    _create_info_plist(contents_dir)

    print(f"[3/4] 创建启动脚本 (使用系统 Python)...")
    _create_launcher(macos_dir, py_ver)

    print(f"[4/4] 嵌入 AutoCat 代码和依赖...")
    _embed_code(resources_dir, site_packages, py_ver)

    # 创建图标
    _create_icon(resources_dir)

    # 签名
    _try_sign(app_dir)

    total_size = sum(f.stat().st_size for f in app_dir.rglob("*") if f.is_file())
    print(f"\n✅ .app 构建完成：{app_dir}")
    print(f"   大小: {total_size / 1024 / 1024:.0f} MB")
    print(f"   拖拽到 Applications 即可使用")

    if create_dmg:
        _build_dmg(app_dir)

    return app_dir


def _create_info_plist(contents_dir: Path):
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDisplayName</key>
    <string>{APP_NAME}</string>
    <key>CFBundleExecutable</key>
    <string>{APP_NAME}</string>
    <key>CFBundleIdentifier</key>
    <string>com.autokat.app</string>
    <key>CFBundleName</key>
    <string>{APP_NAME}</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>{APP_VERSION}</string>
    <key>CFBundleVersion</key>
    <string>{APP_VERSION}</string>
    <key>CFBundleDevelopmentRegion</key>
    <string>zh_CN</string>
    <key>CFBundleIconFile</key>
    <string>autokat</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>LSMinimumSystemVersion</key>
    <string>12.0</string>
    <key>NSHumanReadableCopyright</key>
    <string>MIT License</string>
</dict>
</plist>"""
    (contents_dir / "Info.plist").write_text(plist, encoding="utf-8")


def _create_launcher(macos_dir: Path, py_ver: str):
    """创建启动脚本：使用 .app 内嵌的 Python 解释器"""
    launcher = macos_dir / APP_NAME

    content = f"""#!/bin/bash
# AutoCat 启动器
RESOURCES="$(cd "$(dirname "$0")/../Resources" && pwd)"
# 优先 app 内嵌的 Python.framework
PYTHON_BIN="$RESOURCES/Python.framework/Versions/Current/bin/python3"
if [ ! -f "$PYTHON_BIN" ]; then
    PYTHON_BIN="/usr/bin/python3"
fi

# 不设置 PYTHONHOME，让 Python 使用自己的标准库
# 但把 site-packages 路径加到 PYTHONPATH
export PYTHONPATH="$RESOURCES/lib/python{py_ver}/site-packages:$PYTHONPATH"

# DeepSeek API Key
ENV_FILE="$RESOURCES/.env"
if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
fi

exec "$PYTHON_BIN" << PYEOF
import sys
sys.path.insert(0, "$RESOURCES/lib/python3.14/site-packages")
from autokat.models.db import init_db
from autokat.ui.main_window import run_ui
init_db()
run_ui()
PYEOF

"""
    launcher.write_text(content, encoding="utf-8")
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _embed_code(resources_dir: Path, site_packages: str, py_ver: str):
    """复制 AutoCat 代码和 Python 依赖到 .app 内"""
    lib_dst = resources_dir / "lib" / f"python{py_ver}" / "site-packages"
    lib_dst.mkdir(parents=True, exist_ok=True)

    # ── 复制 AutoCat 核心代码 ──
    print(f"   复制核心代码...")
    code_dst = lib_dst / "autokat"
    if code_dst.exists():
        shutil.rmtree(code_dst)
    shutil.copytree(
        str(PROJECT_DIR / "autokat"),
        str(code_dst),
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )

    # ── 复制 Python 依赖（白名单，排除大包） ──
    print(f"   复制 Python 依赖包...")
    CORE_PKGS = {
        "PySide6", "PySide6_Addons", "PySide6_Essentials",
        "shiboken6",
        "PIL", "Pillow.libs",
        "numpy", "numpy.libs",
        "tqdm",
        "edge_tts",
        "imagehash",
        "soundfile",
        "_soundfile", "_soundfile_data",
        "pydub",
        "PyWavelets",
        "scipy",
        "librosa",
        "lazy_loader",
        "audioread",
        "decorator",
        "pooch",
        "platformdirs",
        "packaging",
        "typing_extensions",
        "certifi",
        "idna",
        "aiohttp", "aiohappyeyeballs", "aiosignal",
        "attrs",
        "attr",
        "frozenlist", "multidict", "propcache", "yarl",
        "cffi", "pycparser",
        "_cffi_backend",
        "tabulate",
        "requests", "urllib3", "charset_normalizer",
    }
    src_site = Path(site_packages)
    for pkg_name in sorted(CORE_PKGS):
        # 支持目录包和 .py 文件包
        # 支持目录、.py 文件、.so 文件
        so_name = f"{pkg_name}.cpython-{py_ver.replace('.','')}-darwin.so"
        src_candidates = [
            src_site / pkg_name,
            src_site / f"{pkg_name}.py",
            src_site / so_name,
        ]
        dst_candidates = [
            lib_dst / pkg_name,
            lib_dst / f"{pkg_name}.py",
            lib_dst / so_name,
        ]
        for src_pkg, dst_pkg in zip(src_candidates, dst_candidates):
            if src_pkg.exists():
                try:
                    if src_pkg.is_dir():
                        if dst_pkg.exists():
                            shutil.rmtree(dst_pkg)
                        shutil.copytree(str(src_pkg), str(dst_pkg),
                                       symlinks=True, dirs_exist_ok=True,
                                       ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                    elif src_pkg.is_file():
                        shutil.copy2(str(src_pkg), str(dst_pkg))
                except Exception as e:
                    print(f"      ⚠️  {pkg_name}: {e}")

    # ── 复制 Python 解释器 ──
    print(f"   复制 Python 解释器...")
    py_bin_src = str(VENV_DIR / "bin" / "python3")
    py_bin_dst = resources_dir / "bin"
    py_bin_dst.mkdir(parents=True, exist_ok=True)
    
    # 复制 python3 二进制及依赖的 .dylib
    _copy_python_with_deps(py_bin_src, py_bin_dst, py_ver)
    
    # ── 复制 FFmpeg ──
    print(f"   复制 FFmpeg...")
    ffmpeg_candidates = [
        "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/usr/bin/ffmpeg",
    ]
    ffmpeg_dst = resources_dir / "ffmpeg"
    ffprobe_dst = resources_dir / "ffprobe"
    for src in ffmpeg_candidates:
        if os.path.exists(src):
            import shutil as _sh
            _sh.copy2(src, str(ffmpeg_dst))
            os.chmod(str(ffmpeg_dst), 0o755)
            print(f"      FFmpeg: {src}")
            # 也复制 ffprobe
            src_probe = src.replace("ffmpeg", "ffprobe")
            if os.path.exists(src_probe):
                _sh.copy2(src_probe, str(ffprobe_dst))
                os.chmod(str(ffprobe_dst), 0o755)
            break
    else:
        print(f"      ⚠️ 未找到 FFmpeg（用户需自行安装）")

    # ── 创建 assets 目录并复制 BGM ──
    print(f"   复制 assets...")
    assets_dst = resources_dir / "assets"
    assets_dst.mkdir(parents=True, exist_ok=True)
    for sub in ["images", "videos", "kenburns", "clips", "tts", "bgm"]:
        (assets_dst / sub).mkdir(exist_ok=True)
    # 复制 BGM 文件
    src_bgm_dir = PROJECT_DIR / "assets" / "bgm"
    if src_bgm_dir.exists():
        for f in src_bgm_dir.iterdir():
            if f.is_file():
                import shutil as _sh
                _sh.copy2(str(f), str(assets_dst / "bgm" / f.name))
        print(f"      BGM 已复制")


def _create_icon(resources_dir: Path):
    """创建应用图标 - 使用预设科技感图标"""
    icon_dst = resources_dir / "autokat.icns"
    # 使用预先生成的科技感图标
    import shutil
    src_icon = PROJECT_DIR / "dist" / "autokat.icns"
    if src_icon.exists():
        shutil.copy2(str(src_icon), str(icon_dst))
        print(f"   ✅ 已使用科技感图标: {icon_dst}")
    else:
        # 回退生成默认图标
        try:
            from PIL import Image, ImageDraw
            img = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.rounded_rectangle([(50, 50), (974, 974)], radius=180, fill="#4A90D9")
            draw.ellipse([(250, 200), (774, 550)], fill="white")
            draw.polygon([(350, 600), (674, 600), (512, 800)], fill="white")
            img.save(str(icon_dst), format="PNG")
            print(f"   已生成默认图标(回退)")
        except Exception as e:
            print(f"   图标生成跳过: {e}")


def _try_sign(app_dir: Path):
    try:
        subprocess.run(
            ["codesign", "--force", "--deep", "--sign", "-", str(app_dir)],
            capture_output=True, timeout=30,
        )
        print(f"   ✅ 已签名")
    except Exception:
        print(f"   ⚠️ 未签名（首次打开需在系统设置中允许运行）")


def _build_dmg(app_dir: Path):
    print(f"\n[DMG] 创建磁盘映像...")
    dmg_path = DIST_DIR / f"{APP_NAME}-{APP_VERSION}.dmg"
    dmg_dir = "/tmp/autokat_dmg_build"

    if os.path.exists(dmg_dir):
        shutil.rmtree(dmg_dir)
    os.makedirs(dmg_dir)

    dst_app = f"{dmg_dir}/{APP_NAME}.app"
    shutil.copytree(str(app_dir), dst_app, symlinks=True)
    os.symlink("/Applications", f"{dmg_dir}/Applications")

    try:
        # 先估算大小
        size_mb = sum(f.stat().st_size for f in app_dir.rglob("*") if f.is_file())
        size_mb = max(500, size_mb // (1024 * 1024) + 200)

        subprocess.run([
            "hdiutil", "create",
            "-volname", APP_NAME,
            "-srcfolder", dmg_dir,
            "-ov",
            "-format", "UDZO",
            "-size", f"{size_mb}m",
            str(dmg_path),
        ], check=True, capture_output=True, timeout=600)

        actual = dmg_path.stat().st_size / 1024 / 1024
        print(f"\n✅ .dmg 构建完成：{dmg_path} ({actual:.0f} MB)")
        print(f"   用户操作：双击 .dmg → 拖拽 AutoCat.app 到 Applications")

    except subprocess.CalledProcessError as e:
        err = e.stderr.decode()[:300]
        print(f"❌ DMG 创建失败: {err}")
    finally:
        shutil.rmtree(dmg_dir, ignore_errors=True)


def _copy_python_with_deps(python_bin: str, dst_dir: Path, py_ver: str):
    """复制完整的 Python.framework（包含标准库）到 .app 的 Resources 目录下"""
    import shutil as _sh

    real_python = os.path.realpath(python_bin)
    
    # 找到 Python.framework 根目录
    # 路径通常是 .../Python.framework/Versions/3.14/bin/python3.14
    framework_root = real_python
    for _ in range(10):
        framework_root = os.path.dirname(framework_root)
        if framework_root.endswith("Python.framework"):
            break
    
    if not framework_root.endswith("Python.framework"):
        print(f"      ⚠️ 找不到 Python.framework，使用简单二进制复制")
        _sh.copy2(real_python, str(dst_dir / "python3"))
        return
    
    # 复制整个 framework（到 Resources/ 下，启动脚本会找 Resources/Python.framework）
    resources_dir = dst_dir.parent  # Resources
    fw_dst = resources_dir / "Python.framework"
    if fw_dst.exists():
        _sh.rmtree(str(fw_dst))
    
    print(f"      Python.framework: {framework_root}")
    
    fw_ver = fw_dst / "Versions" / py_ver
    for sub_dir in ["bin", "lib"]:
        src = Path(framework_root) / "Versions" / py_ver / sub_dir
        dst = fw_ver / sub_dir
        if src.exists():
            _sh.copytree(str(src), str(dst), symlinks=True,
                        ignore=_sh.ignore_patterns("__pycache__", "*.pyc", "test", "tests",
                                                    "tkinter", "idlelib", "lib2to3",
                                                    "turtledemo", "ensurepip", "venv",
                                                    "distutils", "pydoc_data"))
    
    # 创建 Current 符号链接
    (fw_dst / "Versions" / "Current").symlink_to(py_ver)
    
    # 确保 bin/python3 存在
    bin_dir = fw_ver / "bin"
    python3_bin = bin_dir / "python3"
    if not python3_bin.exists():
        # 找 python3.14 并创建符号链接
        for f in bin_dir.iterdir():
            if f.name.startswith("python3."):
                python3_bin.symlink_to(f.name)
                break
    
    print(f"      ✅ Python.framework 已嵌入（含标准库）")
if __name__ == "__main__":
    create_dmg = "--dmg" in sys.argv
    build_app(create_dmg=create_dmg)
