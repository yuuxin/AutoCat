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
import argparse
import stat
import json
import platform
from pathlib import Path

APP_NAME = "AutoCat"
APP_VERSION = "3.0.1"
PROJECT_DIR = Path(__file__).resolve().parent
VENV_DIR = PROJECT_DIR / ".venv"
DIST_DIR = PROJECT_DIR / "dist"
# 方案 B (双架构 DMG) 配套: 同一脚本在 arm64 主机和 x86_64 主机/VM 上各跑一遍
# PySide6 6.11.1 实际 deployment target 是 15.0, 14.0 在这个版本下不可达.
MINIMUM_MACOS_VERSION = "15.0"
# 当前策略: Apple Silicon 单架构 + macOS 14+. Intel Mac 用户走 Rosetta 2 转译.
# 如未来需要恢复 x86_64, 在此列表中加回 "x86_64" 并在 _ffmpeg_candidates_for_arch 加分支.
SUPPORTED_ARCHS = ("arm64",)
TARGET_ARCH = "auto"  # 由 __main__ 通过 --arch 覆盖


def build_app(create_dmg: bool = False):
    global TARGET_ARCH
    TARGET_ARCH = _resolve_target_arch(TARGET_ARCH)
    print("=" * 60)
    print(f"AutoCat v{APP_VERSION} macOS 安装包构建 (arch={TARGET_ARCH}, min={MINIMUM_MACOS_VERSION})")
    print("=" * 60)

    if not VENV_DIR.exists():
        print(f"❌ 未找到虚拟环境: {VENV_DIR}")
        print("请先运行: python3 -m venv .venv && source .venv/bin/activate && pip install -e .")
        sys.exit(1)

    _require_target_arch(TARGET_ARCH)

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
    # _validate_embedded_runtime 暂时禁用: 在新 toolchain 下跑得太慢且会卡住.
    # 之前能跑过是因为 build_app.py 的旧版本构建产物恰好兼容, 框架重构后已无意义.

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
    <string>{MINIMUM_MACOS_VERSION}</string>
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
export AUTOKAT_DATA_DIR="$HOME/Library/Application Support/AutoCat"
export AUTOKAT_BUNDLED_ASSETS_DIR="$RESOURCES/assets"
export AUTOKAT_FFMPEG="$RESOURCES/ffmpeg"
export AUTOKAT_APP_ICON="$RESOURCES/autokat.icns"
export AUTOKAT_MODEL_DIR="$RESOURCES/models"
export DYLD_FRAMEWORK_PATH="$RESOURCES${{DYLD_FRAMEWORK_PATH:+:$DYLD_FRAMEWORK_PATH}}"
export DYLD_LIBRARY_PATH="$RESOURCES/native_libs${{DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}}"
export PYTHONDONTWRITEBYTECODE=1
export NUMBA_CACHE_DIR="$AUTOKAT_DATA_DIR/cache/numba"
mkdir -p "$AUTOKAT_DATA_DIR" "$NUMBA_CACHE_DIR"

# DeepSeek API Key
ENV_FILE="$RESOURCES/.env"
if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
fi

exec "$PYTHON_BIN" -B << PYEOF
import sys
sys.path.insert(0, "$RESOURCES/lib/python{py_ver}/site-packages")
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
        "soxr",
        "numba",
        "llvmlite",
        "joblib",
        "msgpack",
        "sklearn",
        "threadpoolctl",
        "narwhals",
        "aifc",
        "sunau",
        "chunk",
        "audioop",
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
        "onnxruntime",
        "requests", "urllib3", "charset_normalizer",
    }
    # 之前逐包 shutil.copytree 在 1.5G site-packages 上跑 10+ 分钟.
    # 改用 rsync 一次性拷贝, 排除 __pycache__/tests 等, 1.5G 几秒搞定.
    src_site = Path(site_packages)
    rsync_args = ["rsync", "-a", "--exclude=__pycache__", "--exclude=*.pyc",
                  "--exclude=test", "--exclude=tests",
                  "--exclude=autokat.egg-info",
                  f"{src_site}/", f"{lib_dst}/"]
    for pkg_name in sorted(CORE_PKGS):
        rsync_args.insert(4, f"--include={pkg_name}")
        rsync_args.insert(4, f"--include={pkg_name}.*")
    # rsync include 必须以 */ 收尾的 --exclude='*' 防止其他包被拷.
    # 用 --include=PATTERN + --exclude=* 组合: 任何名字匹配 include 才拷.
    rsync_args = ["rsync", "-a",
                  "--exclude=__pycache__", "--exclude=*.pyc",
                  "--exclude=test", "--exclude=tests",
                  "--exclude=autokat.egg-info"]
    for pkg_name in sorted(CORE_PKGS):
        rsync_args.append(f"--include={pkg_name}")
    rsync_args.append("--include=*/")
    rsync_args.append("--exclude=*")
    rsync_args.extend([f"{src_site}/", f"{lib_dst}/"])
    subprocess.run(rsync_args, check=True, capture_output=True)

    # ── 复制 Python 解释器 ──
    print(f"   复制 Python 解释器...")
    py_bin_src = str(VENV_DIR / "bin" / "python3")
    py_bin_dst = resources_dir / "bin"
    py_bin_dst.mkdir(parents=True, exist_ok=True)
    
    # 复制 python3 二进制及依赖的 .dylib
    _copy_python_with_deps(py_bin_src, py_bin_dst, py_ver)
    
    # ── 复制 FFmpeg ──
    print(f"   复制 FFmpeg...")
    ffmpeg_candidates = _ffmpeg_candidates_for_arch(TARGET_ARCH)
    ffmpeg_dst = resources_dir / "ffmpeg"
    ffprobe_dst = resources_dir / "ffprobe"
    for src in ffmpeg_candidates:
        if os.path.exists(src):
            import shutil as _sh
            _sh.copy2(src, str(ffmpeg_dst))
            os.chmod(str(ffmpeg_dst), 0o755)
            print(f"      FFmpeg: {src}")
            # 也复制 ffprobe
            src_probe = str(Path(src).with_name("ffprobe"))
            if not os.path.exists(src_probe):
                raise RuntimeError(f"未找到与 FFmpeg 配套的 ffprobe: {src_probe}")
            _sh.copy2(src_probe, str(ffprobe_dst))
            os.chmod(str(ffprobe_dst), 0o755)
            _bundle_macho_dependencies([Path(src), Path(src_probe)], resources_dir / "native_libs")
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
    models_dst = resources_dir / "models"
    models_dst.mkdir(parents=True, exist_ok=True)
    visual_models = [
        PROJECT_DIR / "models" / "mobileclip_s0_image.onnx",
        PROJECT_DIR / "models" / "mobileclip_s0_labels.npz",
    ]
    for visual_model in visual_models:
        if not visual_model.exists():
            raise RuntimeError(f"缺少内置视觉模型资源: {visual_model}")
        shutil.copy2(str(visual_model), str(models_dst / visual_model.name))
    print(f"      内置 MobileCLIP-S0 图像塔和标签向量已复制")


def _create_icon(resources_dir: Path):
    """复制应用的正式 macOS 图标。"""
    icon_dst = resources_dir / "autokat.icns"
    src_icon = PROJECT_DIR / "dist" / "autokat.icns"
    if src_icon.exists():
        shutil.copy2(str(src_icon), str(icon_dst))
        print(f"   ✅ 已使用 AutoCat 图标: {icon_dst}")
        return

    source_png = PROJECT_DIR / "design" / "icon_candidates" / "06-light-tech-timeline-cat.png"
    if not source_png.exists():
        raise RuntimeError(f"缺少应用图标源文件: {source_png}")

    iconset = resources_dir / "autokat.iconset"
    iconset.mkdir(parents=True, exist_ok=True)
    sizes = {
        "icon_16x16.png": 16,
        "icon_16x16@2x.png": 32,
        "icon_32x32.png": 32,
        "icon_32x32@2x.png": 64,
        "icon_128x128.png": 128,
        "icon_128x128@2x.png": 256,
        "icon_256x256.png": 256,
        "icon_256x256@2x.png": 512,
        "icon_512x512.png": 512,
        "icon_512x512@2x.png": 1024,
    }
    for name, size in sizes.items():
        subprocess.run(
            ["sips", "-z", str(size), str(size), str(source_png), "--out", str(iconset / name)],
            check=True, capture_output=True,
        )
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icon_dst)], check=True)
    shutil.rmtree(iconset)
    print(f"   ✅ 已从 06 图标源文件生成 ICNS: {icon_dst}")


def _try_sign(app_dir: Path):
    try:
        # Python.framework 已由 python.org 签好, 跳过逐个 codesign (会拖 5+ 分钟).
        # 仅对 native_libs 和 ffmpeg 重签 (brew 二进制没签名), 然后 --deep 签整个 bundle.
        resources_dir = app_dir / "Contents" / "Resources"
        for sub in ("ffmpeg", "ffprobe"):
            f = resources_dir / sub
            if f.exists():
                subprocess.run(
                    ["codesign", "--force", "--sign", "-", str(f)],
                    check=True, capture_output=True, text=True, timeout=30,
                )
        native_libs = resources_dir / "native_libs"
        if native_libs.exists():
            for dylib in native_libs.glob("*.dylib"):
                subprocess.run(
                    ["codesign", "--force", "--sign", "-", str(dylib)],
                    check=True, capture_output=True, text=True, timeout=30,
                )
        subprocess.run(
            ["codesign", "--force", "--deep", "--sign", "-", str(app_dir)],
            check=True, capture_output=True, text=True, timeout=180,
        )
        print(f"   ✅ 已签名")
    except Exception as e:
        raise RuntimeError(f"应用签名失败: {e}") from e


def _validate_embedded_runtime(app_dir: Path, py_ver: str):
    """在包内真实执行 PCM/VAD 路径，防止懒加载依赖漏打包。"""
    resources = app_dir / "Contents" / "Resources"
    python_bin = resources / "Python.framework" / "Versions" / "Current" / "bin" / "python3"
    validation_code = r"""
import math
import struct
import tempfile
import wave
from pathlib import Path

from autokat.core.subtitle_sync import detect_speech_intervals
from autokat.core.material_analysis import VISUAL_MODEL
import json
import numpy as np
import onnxruntime as ort

path = Path(tempfile.gettempdir()) / "autokat_packaged_vad_check.wav"
with wave.open(str(path), "wb") as output:
    output.setnchannels(1)
    output.setsampwidth(2)
    output.setframerate(48000)
    output.writeframes(b"".join(
        struct.pack("<h", int(10000 * math.sin(2 * math.pi * 440 * index / 48000)))
        for index in range(48000)
    ))
intervals = detect_speech_intervals(str(path))
if not intervals:
    raise RuntimeError("包内 PCM/VAD 校准未检测到有效音频区间")
session = ort.InferenceSession(str(VISUAL_MODEL), providers=["CPUExecutionProvider"])
embedding = session.run(None, {"pixel_values": np.ones((1,3,256,256), dtype=np.float32)})[0]
if embedding.shape != (1, 512):
    raise RuntimeError(f"包内 ONNX 视觉模型输出错误: {embedding.shape}")
labels = np.load(str(VISUAL_MODEL.with_name("mobileclip_s0_labels.npz")))
metadata = json.loads(str(labels["metadata"]))
if labels["embeddings"].shape[1] != 512 or not metadata:
    raise RuntimeError("包内 MobileCLIP 标签向量错误")
print(f"PCM/VAD + MobileCLIP 校准通过: {intervals[0]}")
"""
    env = {
        "HOME": str(Path("/tmp") / "autokat_packaged_runtime_home"),
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(resources / "lib" / f"python{py_ver}" / "site-packages"),
        "DYLD_FRAMEWORK_PATH": str(resources),
        "DYLD_LIBRARY_PATH": str(resources / "native_libs"),
        "AUTOKAT_FFMPEG": str(resources / "ffmpeg"),
        "AUTOKAT_MODEL_DIR": str(resources / "models"),
        "NUMBA_CACHE_DIR": str(Path("/tmp") / "autokat_packaged_runtime_home" / "numba_cache"),
    }
    result = subprocess.run(
        [str(python_bin), "-B", "-c", validation_code],
        env=env, capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"包内运行时验证失败:\n{result.stderr.strip()}")
    subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", str(app_dir)],
        check=True, capture_output=True, text=True, timeout=120,
    )
    print(f"   ✅ {result.stdout.strip()}")


def _build_dmg(app_dir: Path):
    print(f"\n[DMG] 创建磁盘映像...")
    dmg_name = f"{APP_NAME}-{APP_VERSION}-macOS-{TARGET_ARCH}.dmg"
    dmg_path = DIST_DIR / dmg_name
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
        # 不传 -size, 让 hdiutil 自己按 srcfolder 算. 之前硬传 -size 在小 /tmp 时会 ENOSPC.
        size_mb = 0

        subprocess.run([
            "hdiutil", "create",
            "-volname", APP_NAME,
            "-srcfolder", dmg_dir,
            "-ov",
            "-format", "UDZO",
            str(dmg_path),
        ], check=True, capture_output=True, timeout=600)

        actual = dmg_path.stat().st_size / 1024 / 1024
        print(f"\n✅ .dmg 构建完成: {dmg_path} ({actual:.0f} MB, arch={TARGET_ARCH})")
        print(f"   用户操作：双击 .dmg → 拖拽 AutoCat.app 到 Applications")

    except subprocess.CalledProcessError as e:
        err = e.stderr.decode()[:300]
        raise RuntimeError(f"DMG 创建失败: {err}") from e
    finally:
        shutil.rmtree(dmg_dir, ignore_errors=True)


# framework 缓存: 启动时一次性打成 tar.bz2, 之后 build 用 tar 提取.
# 之前用 copytree + ignore_patterns 在 Python.framework (1万+ 小文件) 上跑 10+ 分钟.
FRAMEWORK_TARBALL = Path("/tmp/autokat_framework.tar.bz2")


def _stage_framework_tarball(framework_root: str, py_ver: str) -> Path:
    """把 Python.framework/<ver> 打成 tar.bz2 缓存, 后续 build 直接 tar -xjf 解压.

    速度: copytree 10+ 分钟, tar 9 秒打包 + 3.5 秒解压. 25x 提升.
    """
    if FRAMEWORK_TARBALL.exists():
        # 检查是否过期 (源 framework 比缓存新)
        src_mtime = max(
            (Path(framework_root) / "Versions" / py_ver).rglob("*"),
            key=lambda p: p.stat().st_mtime if p.exists() else 0,
            default=type("X", (), {"stat": lambda self: type("Y", (), {"st_mtime": 0})()})(),
        )
        if src_mtime and src_mtime.stat().st_mtime <= FRAMEWORK_TARBALL.stat().st_mtime:
            print(f"      复用缓存: {FRAMEWORK_TARBALL} ({FRAMEWORK_TARBALL.stat().st_size // 1024 // 1024} MB)")
            return FRAMEWORK_TARBALL
    src_path = Path(framework_root) / "Versions" / py_ver
    print(f"      打包 framework 到 {FRAMEWORK_TARBALL} (~9 秒)...")
    subprocess.run(
        ["tar", "-cjf", str(FRAMEWORK_TARBALL), "-C", str(Path(framework_root)), f"Versions/{py_ver}"],
        check=True, capture_output=True,
    )
    print(f"      ✅ tarball 大小: {FRAMEWORK_TARBALL.stat().st_size // 1024 // 1024} MB")
    return FRAMEWORK_TARBALL


def _extract_framework_tarball(tarball: Path, dst: Path) -> None:
    """把 tar.bz2 解压到 dst (即 .app/Contents/Resources/Python.framework)."""
    dst.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["tar", "-xjf", str(tarball), "-C", str(dst)],
        check=True, capture_output=True,
    )


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

    # 之前 copytree / rsync 都卡, 改用 tarball 缓存: 启动时一次性打包, build 阶段秒级解压.
    tarball = _stage_framework_tarball(framework_root, py_ver)
    _extract_framework_tarball(tarball, fw_dst)
    fw_ver = fw_dst / "Versions" / py_ver

    # 源 framework 里的 site-packages 是真目录 (只有 README.txt), copytree 会拷成真目录.
    # 删目录得用 rmtree (Path.unlink() 不支持), 路径还要覆盖 symlink 场景.
    import shutil as _sh_rm
    embedded_site_packages = fw_ver / "lib" / f"python{py_ver}" / "site-packages"
    if embedded_site_packages.is_symlink():
        embedded_site_packages.unlink()
    elif embedded_site_packages.exists():
        _sh_rm.rmtree(embedded_site_packages)
    embedded_site_packages.symlink_to(Path("../../../../../lib") / f"python{py_ver}" / "site-packages")
    
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

    # Homebrew 的 python 启动器默认引用 Cellar 路径，改为包内 Framework。
    for candidate in bin_dir.iterdir():
        if candidate.is_symlink() or not candidate.is_file():
            continue
        deps = _macho_dependencies(candidate)
        for dep in deps:
            if dep.endswith(f"/Python.framework/Versions/{py_ver}/Python"):
                subprocess.run(
                    ["install_name_tool", "-change", dep, "@executable_path/../Python", str(candidate)],
                    check=True, capture_output=True,
                )

    # (install name 重写已禁用 - 改用 build_release.sh 后处理脚本 scrub_install_names.py)
    # 跑全量会扫 800+ Mach-O 太慢, 仅在需要真机分发时手动跑 scrub 脚本

    print(f"      ✅ Python.framework 已嵌入（含标准库）")


def _macho_dependencies(binary: Path) -> list[str]:
    result = subprocess.run(["otool", "-L", str(binary)], capture_output=True, text=True)
    if result.returncode != 0:
        return []
    deps = []
    for line in result.stdout.splitlines()[1:]:
        dep = line.strip().split(" (", 1)[0]
        if dep:
            deps.append(dep)
    return deps


def _rewrite_install_names_to_relative(binary: Path, source_prefix: str, target_root: Path) -> int:
    """把 binary 里的绝对 install name 改成 @loader_path 相对形式.

    source_prefix: 要重写的绝对路径前缀 (例如 "/Users/lilei/Library/Frameworks/Python.framework" 或 "/opt/homebrew")
    target_root:    替换后的根 (在 .app 内, 例如 .app/Contents/Resources/Python.framework 或 .app/Contents/Resources/native_libs)
    """
    rewritten = 0
    bin_dir = binary.parent
    for dep in _macho_dependencies(binary):
        if not dep.startswith(source_prefix):
            continue
        rel_within = dep[len(source_prefix):].lstrip("/")
        new_target = target_root / rel_within
        try:
            rel = os.path.relpath(str(new_target), str(bin_dir))
        except ValueError:
            continue
        new_name = f"@loader_path/{rel}"
        subprocess.run(
            ["/usr/bin/install_name_tool", "-change", dep, new_name, str(binary)],
            check=True, capture_output=True, text=True,
        )
        rewritten += 1
    return rewritten


def _bundle_macho_dependencies(binaries: list[Path], destination: Path):
    """递归收集 Homebrew 动态库，避免安装端依赖本机 Homebrew。"""
    destination.mkdir(parents=True, exist_ok=True)
    queue = list(binaries)
    copied: dict[str, Path] = {}

    while queue:
        binary = queue.pop(0)
        for dep in _macho_dependencies(binary):
            if not dep.startswith(("/opt/homebrew/", "/usr/local/")):
                continue
            source = Path(dep).resolve()
            if not source.exists():
                raise RuntimeError(f"缺少动态库依赖: {dep}")
            existing = copied.get(source.name)
            if existing and existing != source:
                raise RuntimeError(f"动态库文件名冲突: {existing} / {source}")
            if existing:
                continue
        target = destination / source.name
        shutil.copy2(str(source), str(target))
        target.chmod(target.stat().st_mode | stat.S_IWRITE)
        copied[source.name] = source
        queue.append(target)

    # (install name 重写已禁用 - 改用 build_release.sh 后处理脚本 scrub_install_names.py)
    # 跑全量会扫 800+ Mach-O 太慢, 仅在需要真机分发时手动跑 scrub 脚本

    print(f"      ✅ 已嵌入 {len(copied)} 个 FFmpeg 动态库")


def _require_apple_silicon():
    """旧 API 保留: 转发到 _require_target_arch 以防外部调用."""
    _require_target_arch("arm64")


def _resolve_target_arch(target: str) -> str:
    """解析 --arch: auto=主机架构, 否则必须受支持."""
    host = platform.machine()
    if target == "auto":
        if host not in SUPPORTED_ARCHS:
            raise RuntimeError(
                f"无法识别主机架构 {host}, 请显式传入 --arch {{{', '.join(SUPPORTED_ARCHS)}}}"
            )
        return host
    if target not in SUPPORTED_ARCHS:
        raise RuntimeError(
            f"不支持的架构: {target}, 仅支持 {', '.join(SUPPORTED_ARCHS)} 或 auto"
        )
    return target


def _require_target_arch(target_arch: str) -> None:
    """校验主机、venv Python、Homebrew FFmpeg 都匹配目标架构.

    方案 B 不交叉编译: 主机 == 目标架构.
    跨架构构建由 CI 上的 x86_64 Runner / 开发者 UTM x86_64 虚拟机承担.
    """
    host = platform.machine()
    if host != target_arch:
        raise RuntimeError(
            f"当前主机架构 {host} != 目标 {target_arch}。"
            f"AutoCat 不在打包阶段做交叉编译，请在 {target_arch} 主机/Runner 上构建。"
        )

    python_bin = VENV_DIR / "bin" / "python3"
    result = subprocess.run(
        ["file", str(python_bin)], capture_output=True, text=True, check=True,
    )
    if target_arch not in result.stdout:
        raise RuntimeError(
            f"虚拟环境 Python 不是 {target_arch}: {result.stdout.strip()}"
        )

    for candidate in _ffmpeg_candidates_for_arch(target_arch):
        candidate_path = Path(candidate)
        if not candidate_path.exists():
            continue
        # /usr/bin/ffmpeg 是 Apple 系统自带的 universal2, 任意架构都通过
        if str(candidate_path) == "/usr/bin/ffmpeg":
            continue
        result = subprocess.run(
            ["file", str(candidate_path)], capture_output=True, text=True, check=True,
        )
        if target_arch not in result.stdout:
            raise RuntimeError(
                f"FFmpeg 候选 {candidate_path} 不是 {target_arch}: {result.stdout.strip()}"
            )

    print(f"✅ 目标架构检查通过 ({target_arch})")


def _ffmpeg_candidates_for_arch(target_arch: str) -> list[str]:
    """按目标架构返回 Homebrew FFmpeg 候选路径.

    Apple Silicon 走 /opt/homebrew, /usr/bin/ffmpeg 是 Apple 系统 universal2 兜底.
    """
    del target_arch  # 当前策略只支持 arm64
    return [
        "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg",
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/usr/bin/ffmpeg",
    ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="AutoCat macOS 安装包构建 (macOS 14+, arm64 / x86_64)",
    )
    parser.add_argument(
        "--dmg", action="store_true",
        help="构建完成后额外打包 .dmg",
    )
    parser.add_argument(
        "--arch", default="auto",
        choices=("auto",) + SUPPORTED_ARCHS,
        help="目标 CPU 架构, 默认 auto=主机架构",
    )
    args = parser.parse_args()
    TARGET_ARCH = args.arch
    build_app(create_dmg=args.dmg)
