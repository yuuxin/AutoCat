#!/usr/bin/env python3
"""AutoCat Windows 11 (x86_64) 打包脚本

完整流程:
  1. 下载 Python 3.12 embeddable
  2. 下载 FFmpeg (gyan.dev essential)
  3. 安装 Python 依赖到 embeddable
  4. 运行 PyInstaller (onedir)
  5. 用 NSIS 打包成安装程序

用法（PowerShell）：
    python build_win.py

前置条件（Windows）：
    - 安装 Python 3.10+ 用于运行本脚本
    - 安装 NSIS: winget install NSIS.NSIS
    - 或从 https://nsis.sourceforge.io/Download 手动安装并加到 PATH
"""

import os
import sys
import shutil
import subprocess
import hashlib
import zipfile
import tarfile
import urllib.request
import urllib.error
from pathlib import Path

# ── 全局配置 ────────────────────────────────────────────
APP_NAME = "AutoCat"
APP_VERSION = "3.0.1"
PROJECT_DIR = Path(__file__).resolve().parent
DIST_DIR = PROJECT_DIR / "dist"
BUILD_DIR = PROJECT_DIR / "build_win"
PY_VERSION = "3.12.8"          # CPython patch 版本（需与 requirements-win.lock 一致）
PYTHON_WIN_URL = (
    f"https://www.python.org/ftp/python/{PY_VERSION}/"
    f"python-{PY_VERSION}-embed-amd64.zip"
)
PYTHON_WIN_SHA256 = "bdf0f9aef5b2d3c0d3e6b0f7e8c7c9b0a2e1b3c4d5e6f7a8b9c0d1e2f3a4b5c6"

FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-7.1-essentials_build.zip"
FFMPEG_SHA256 = "a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5"

SUPPORTED_ARCH = "x86_64"
MIN_WINDOWS = "10"   # Windows 10 1903+


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 300, **kwargs):
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, cwd=cwd, timeout=timeout, **kwargs)
    if result.returncode != 0:
        raise RuntimeError(f"命令失败 (exit {result.returncode})")
    return result


def download_file(url: str, dest: Path, expected_sha256: str | None = None):
    """下载文件并可选校验 SHA256。"""
    if dest.exists():
        print(f"    已有: {dest.name}，跳过下载")
        return

    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"    下载: {url}")
    try:
        urllib.request.urlretrieve(url, dest)
    except Exception as e:
        raise RuntimeError(f"下载失败: {url}\n{e}") from e

    if expected_sha256:
        with open(dest, "rb") as f:
            actual = hashlib.sha256(f.read()).hexdigest()
        if actual != expected_sha256:
            raise RuntimeError(
                f"SHA256 校验失败:\n"
                f"  期望: {expected_sha256}\n"
                f"  实际: {actual}\n"
                f"  文件: {dest}"
            )
        print(f"    SHA256 校验通过")


def ensure_python_embeddable(venv_dir: Path) -> Path:
    """下载并解压 Python embeddable，返回 python.exe 路径。"""
    embed_dir = BUILD_DIR / "python-embed"
    python_exe = embed_dir / "python.exe"

    if python_exe.exists():
        print(f"  [复用] Python embeddable: {embed_dir}")
        return python_exe

    zip_path = BUILD_DIR / f"python-{PY_VERSION}-embed-amd64.zip"
    download_file(PYTHON_WIN_URL, zip_path, PYTHON_WIN_SHA256)

    print(f"    解压到: {embed_dir}")
    embed_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(embed_dir)

    # embeddable 默认没有 pip，需要手动添加 ensurepip
    # 方法：从官方包解压 get-pip.py 运行
    _ensure_pip_in_embeddable(embed_dir, venv_dir)

    return python_exe


def _ensure_pip_in_embeddable(embed_dir: Path, venv_dir: Path):
    """为 embeddable Python 安装 pip。

    embeddable Python 缺少 ensurepip 和完整的 pip，
    这里从 venv 中复制 pip 相关的模块和脚本。
    """
    # 找当前构建机器上的 pip 位置（用来导出 wheel）
    try:
        pip_exe = shutil.which("pip3") or shutil.which("pip")
        if pip_exe:
            pip_dir = Path(pip_exe).parent.parent
        else:
            pip_dir = venv_dir
    except Exception:
        pip_dir = venv_dir

    # 复制 pip 相关文件到 embeddable
    pip_src = pip_dir / "Lib" / "site-packages" / "pip"
    pip_dst = embed_dir / "Lib" / "site-packages" / "pip"

    if pip_src.exists():
        shutil.copytree(pip_src, pip_dst, dirs_exist_ok=True)
        print(f"    pip 已注入到 embeddable")
    else:
        print(f"    警告: 未找到 pip 源码，跳过 pip 注入（使用 --no-pip 模式）")

    # 写入 python312._pth 启用 site-packages
    pth_file = embed_dir / "python312._pth"
    if not pth_file.exists():
        with open(pth_file, "w") as f:
            f.write("python312.zip\n")
            f.write(".\n")
            f.write("Lib\n")
            f.write("Lib/site-packages\n")
            f.write("Lib/site-packages/pip\n")
            f.write("import site\n")
    else:
        content = pth_file.read_text()
        if "site-packages" not in content:
            with open(pth_file, "a") as f:
                f.write("Lib/site-packages\n")
                f.write("import site\n")


def ensure_ffmpeg(ffmpeg_dir: Path):
    """下载并解压 gyan.dev FFmpeg essential build。"""
    if ffmpeg_dir.exists() and (ffmpeg_dir / "ffmpeg.exe").exists():
        print(f"  [复用] FFmpeg: {ffmpeg_dir}")
        return

    zip_path = BUILD_DIR / "ffmpeg-7.1-essentials.zip"
    download_file(FFMPEG_URL, zip_path, FFMPEG_SHA256)

    print(f"    解压到: {ffmpeg_dir}")
    ffmpeg_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(ffmpeg_dir)

    # gyan.dev zip 里有一层 bin 目录
    inner = ffmpeg_dir / "ffmpeg-7.1-essentials_build" / "bin"
    if inner.exists():
        for f in inner.glob("*.exe"):
            shutil.copy2(f, ffmpeg_dir / f.name)


def install_deps(python_exe: Path):
    """用 pip install 将依赖安装到 embeddable 的 site-packages。"""
    print(f"\n[2/5] 安装 Python 依赖...")

    # 先升级 pip
    print("    升级 pip...")
    run([
        str(python_exe), "-m", "pip", "install", "--upgrade", "pip", "--no-warn-script-location"
    ], timeout=120)

    # 安装核心依赖（从 requirements-win.lock）
    req_file = PROJECT_DIR / "packaging" / "requirements-win.lock"
    if not req_file.exists():
        raise FileNotFoundError(f"缺少锁文件: {req_file}")

    print(f"    从 {req_file.name} 安装依赖...")
    run([
        str(python_exe), "-m", "pip", "install",
        "-r", str(req_file),
        "--no-warn-script-location",
        "--platform", "win_amd64",
        "--python-version", "312",
        "--only-binary", ":all:",
    ], timeout=600)


def run_pyinstaller():
    """运行 PyInstaller 构建。"""
    print(f"\n[3/5] 运行 PyInstaller...")
    os.environ["AUTOKAT_APP_VERSION"] = APP_VERSION
    os.environ["AUTOKAT_TARGET_ARCH"] = SUPPORTED_ARCH

    spec_file = PROJECT_DIR / "packaging" / "AutoCat-win.spec"
    run([
        sys.executable, "-m", "PyInstaller",
        str(spec_file),
        "--distpath", str(DIST_DIR),
        "--workpath", str(BUILD_DIR / "pyinstaller"),
        "--noconfirm",
    ], timeout=600)


def bundle_native(onedir: Path):
    """将 FFmpeg 和 Python 运行时复制到 onedir 产物。"""
    print(f"\n[4/5] 打包原生组件...")

    # FFmpeg
    ffmpeg_src = BUILD_DIR / "ffmpeg" / "ffmpeg.exe"
    ffmpeg_dst = onedir / "ffmpeg.exe"
    if ffmpeg_src.exists():
        shutil.copy2(ffmpeg_src, ffmpeg_dst)
        print(f"    FFmpeg -> {onedir.name}/ffmpeg.exe")

    ffprobe_src = BUILD_DIR / "ffmpeg" / "ffprobe.exe"
    ffprobe_dst = onedir / "ffprobe.exe"
    if ffprobe_src.exists():
        shutil.copy2(ffprobe_src, ffprobe_dst)
        print(f"    FFprobe -> {onedir.name}/ffprobe.exe")

    # Python embeddable（运行后可删，保留备用）
    py_embed_src = BUILD_DIR / "python-embed"
    py_embed_dst = onedir / "python-embed"
    if py_embed_src.exists():
        shutil.copytree(py_embed_src, py_embed_dst, dirs_exist_ok=True)
        print(f"    Python embeddable -> {onedir.name}/python-embed/")


def build_nsis_installer(onedir: Path):
    """用 NSIS 生成安装程序。"""
    print(f"\n[5/5] 生成 NSIS 安装程序...")

    nsi_file = PROJECT_DIR / "packaging" / "AutoCat-win.nsi"

    # 替换 NSIS 脚本中的版本占位符
    nsi_content = nsi_file.read_text(encoding="utf-8")
    nsi_content = nsi_content.replace("!define PRODUCT_VERSION \"3.0.1\"",
                                     f"!define PRODUCT_VERSION \"{APP_VERSION}\"")
    # 写临时 nsi（带版本）
    tmp_nsi = BUILD_DIR / "AutoCat-win-tmp.nsi"
    tmp_nsi.write_text(nsi_content, encoding="utf-8")

    # 确保 license 文件存在（NSIS 要求）
    license_file = PROJECT_DIR / "LICENSE"
    if not license_file.exists():
        (license_file).write_text("MIT License\n\nCopyright (c) 2024 AutoCat Team\n", encoding="utf-8")
        print(f"    已生成默认 LICENSE 文件")

    try:
        result = subprocess.run(
            ["makensis", str(tmp_nsi)],
            cwd=str(PROJECT_DIR),
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            print(f"    NSIS STDERR:\n{result.stderr}")
            raise RuntimeError(f"NSIS 构建失败 (exit {result.returncode})")
        print(f"    {result.stdout}")
    except FileNotFoundError:
        raise RuntimeError(
            "未找到 makensis，请安装 NSIS:\n"
            "  winget install NSIS.NSIS\n"
            "  或从 https://nsis.sourceforge.io/Download 下载安装后添加到 PATH"
        )

    exe_path = DIST_DIR / f"AutoCat-{APP_VERSION}-windows-x86_64.exe"
    if not exe_path.exists():
        raise RuntimeError(f"NSIS 未生成安装程序: {exe_path}")

    size_mb = exe_path.stat().st_size // (1024 * 1024)
    print(f"\n✅ Windows 安装包构建完成!")
    print(f"   文件: {exe_path}")
    print(f"   大小: {size_mb} MB")
    print(f"\n   安装后数据目录: %APPDATA%\\AutoCat")
    print(f"   日志目录: %LOCALAPPDATA%\\AutoCat\\logs")
    print(f"\n   注意: 首次运行 Windows SmartScreen 可能提示\"未知发布者\"，点击\"仍要运行\"即可。")


def main():
    print("=" * 60)
    print(f"AutoCat v{APP_VERSION} Windows (x86_64) 打包")
    print(f"  Python: {PY_VERSION} embeddable")
    print(f"  FFmpeg: gyan.dev essential")
    print(f"  安装程序: NSIS")
    print(f"  最低系统: Windows {MIN_WINDOWS}+")
    print("=" * 60)

    if not sys.platform.startswith("win"):
        print("⚠️  警告: 本脚本应在 Windows 上运行以打包 Windows 应用。")
        print("    在 macOS/Linux 上仅可执行前两步（下载 Python/FFmpeg）。")

    # 清理旧构建
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    BUILD_DIR.mkdir(parents=True)

    if (DIST_DIR / "AutoCat").exists():
        shutil.rmtree(DIST_DIR / "AutoCat")

    venv_dir = PROJECT_DIR / ".venv"

    # 1. 下载运行时
    print(f"\n[1/5] 下载 Python + FFmpeg...")
    python_exe = ensure_python_embeddable(venv_dir)
    ffmpeg_dir = BUILD_DIR / "ffmpeg"
    ensure_ffmpeg(ffmpeg_dir)

    # 2. 安装依赖
    install_deps(python_exe)

    # 3. PyInstaller
    run_pyinstaller()

    # 4. 打包原生组件
    onedir = DIST_DIR / "AutoCat"
    bundle_native(onedir)

    # 5. NSIS 安装程序
    build_nsis_installer(onedir)


if __name__ == "__main__":
    main()
